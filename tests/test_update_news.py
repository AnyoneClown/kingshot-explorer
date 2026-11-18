import asyncio
from types import SimpleNamespace

import discord
import pytest

from db.models import GuildConfiguration
from handlers.update_news_handler import (
    NEWS_FOOTER,
    UpdateNewsHandler,
    describe_destination,
    general_channel,
    keeps_code_spans,
)
from services import update_news
from services.guild_configuration_service import GuildConfigurationService
from services.update_news import UpdateNote, parse_update_news, pending_notes
from tests.sqlite_helpers import SqliteDb

NOTES = tuple(UpdateNote(f"note-{number}", f"Title {number}", f"Text {number} with `/settings`") for number in range(1, 6))


# ------------------------------------------------------------------ the file


def test_the_bundled_news_file_is_valid_and_has_something_to_say():
    notes = parse_update_news(update_news.UPDATE_NEWS_PATH.read_text(encoding="utf-8"))

    assert notes and len({note.id for note in notes}) == len(notes)
    update_news.load_update_news.cache_clear()
    assert update_news.load_update_news() == notes


def test_notes_are_read_in_file_order():
    notes = parse_update_news(
        '[[update]]\nid = "first"\ntitle = "One"\ntext = """\nLine\n- item"""\n'
        '[[update]]\nid = "second-2"\ntitle = " Two "\ntext = "More"\n'
    )

    assert notes == (UpdateNote("first", "One", "Line\n- item"), UpdateNote("second-2", "Two", "More"))
    assert parse_update_news("") == ()


@pytest.mark.parametrize(
    ("raw", "problem"),
    [
        ('[[update]]\nid = "a"\ntitle = "T"\ntext = "x"\n[[update]]\nid = "a"\ntitle = "T"\ntext = "x"', "used twice"),
        ('[[update]]\nid = "Has Space"\ntitle = "T"\ntext = "x"', "id must be"),
        ('[[update]]\ntitle = "T"\ntext = "x"', "id must be"),
        ('[[update]]\nid = "a"\ntitle = ""\ntext = "x"', "title must have"),
        ('[[update]]\nid = "a"\ntitle = "T"\ntext = "' + "x" * 901 + '"', "text must have"),
        ("[[update]\nid =", "not valid TOML"),
    ],
)
def test_a_malformed_news_file_is_refused_with_the_reason(raw, problem):
    with pytest.raises(ValueError, match=problem):
        parse_update_news(raw)


def test_a_broken_news_file_means_no_news_not_a_bot_that_cannot_start(tmp_path, monkeypatch):
    broken = tmp_path / "update_news.toml"
    broken.write_text("[[update]\n")
    monkeypatch.setattr(update_news, "UPDATE_NEWS_PATH", broken)
    update_news.load_update_news.cache_clear()
    try:
        assert update_news.load_update_news() == ()
        monkeypatch.setattr(update_news, "UPDATE_NEWS_PATH", tmp_path / "missing.toml")
        update_news.load_update_news.cache_clear()
        assert update_news.load_update_news() == ()
    finally:
        update_news.load_update_news.cache_clear()


def test_pending_notes_are_what_comes_after_the_servers_marker():
    ids = lambda notes: [note.id for note in notes]  # noqa: E731

    assert ids(pending_notes(NOTES, "note-3")) == ["note-4", "note-5"]
    assert pending_notes(NOTES, "note-5") == []
    # Far behind: the newest few, oldest first.
    assert ids(pending_notes(NOTES, "note-1")) == ["note-3", "note-4", "note-5"]
    # New to update news: the newest note, never the history.
    assert ids(pending_notes(NOTES, None)) == ["note-5"]
    # A marker that was withdrawn from the file: nothing, rather than old news again.
    assert pending_notes(NOTES, "gone") == []
    assert pending_notes((), None) == []


# ----------------------------------------------------------------- the marker


def test_the_news_marker_moves_only_from_the_value_the_caller_saw():
    db = SqliteDb()
    GuildConfiguration.__table__.create(db.engine)
    service = GuildConfigurationService(db_manager=db)

    async def run_test():
        fresh = await service.get_settings(1)
        results = [
            await service.claim_update_news(1, None, "note-1"),
            # A second instance that read the same settings loses.
            await service.claim_update_news(1, None, "note-1"),
            await service.claim_update_news(1, "note-1", "note-2"),
            # A server without a row yet can be claimed too.
            await service.claim_update_news(2, None, "note-2"),
        ]
        moved = await service.get_settings(1)
        given_back = await service.claim_update_news(1, "note-2", None)
        return fresh, results, moved, given_back, await service.get_settings(1)

    fresh, results, moved, given_back, after = asyncio.run(run_test())

    assert (fresh.update_news_enabled, fresh.update_news_channel_id, fresh.update_news_last_id) == (True, None, None)
    assert results == [True, False, True, True]
    assert moved.update_news_last_id == "note-2"
    assert given_back is True and after.update_news_last_id is None


# ------------------------------------------------------------------- posting


class FakeChannel:
    def __init__(self, channel_id, name="general", fail=False, closed=False):
        self.id = channel_id
        self.name = name
        self.sent = []
        self._fail = fail
        if closed:
            # A real channel the bot may not write in.
            self.guild = SimpleNamespace(me=object())
            self.permissions_for = lambda me: SimpleNamespace(
                view_channel=True, send_messages=False, embed_links=True
            )

    async def send(self, *, embeds, allowed_mentions):
        assert allowed_mentions.everyone is False and allowed_mentions.users is False
        if self._fail and len(self.sent) >= self._fail - 1:
            raise discord.HTTPException(SimpleNamespace(status=500, reason="boom"), "boom")
        self.sent.append(embeds)


class FakeGuild:
    def __init__(self, guild_id=1, system_channel=None, text_channels=()):
        self.id = guild_id
        self.system_channel = system_channel
        self.text_channels = list(text_channels)

    def get_channel(self, channel_id):
        channels = [self.system_channel, *self.text_channels]
        return next((channel for channel in channels if channel is not None and channel.id == channel_id), None)


class FakeSettingsService:
    def __init__(self, **settings):
        self.settings = {}
        self.defaults = settings
        self.lose_claims = False

    def _row(self, guild_id):
        return self.settings.setdefault(
            guild_id,
            SimpleNamespace(
                **{
                    "update_news_enabled": True,
                    "update_news_channel_id": None,
                    "update_news_last_id": None,
                    "translation_languages": None,
                    **self.defaults,
                }
            ),
        )

    async def get_settings(self, guild_id):
        return SimpleNamespace(**vars(self._row(guild_id)))

    async def claim_update_news(self, guild_id, seen_id, new_id):
        row = self._row(guild_id)
        if self.lose_claims or row.update_news_last_id != seen_id:
            return False
        row.update_news_last_id = new_id
        return True

    async def update_for_guild(self, guild_id, **changes):
        vars(self._row(guild_id)).update(changes)


class FakeTranslator:
    def __init__(self, broken=(), drop_code=()):
        self.calls = []
        self._broken = set(broken)
        self._drop_code = set(drop_code)

    async def translate_to_language(self, text, language):
        self.calls.append((text, language))
        if language in self._broken:
            raise RuntimeError("model is down")
        translated = f"<{language}> {text}"
        return {"text": translated.replace("`/settings`", "`/налаштування`") if language in self._drop_code else translated}


def post(guild, service, *, translator=None, notes=NOTES):
    handler = UpdateNewsHandler(SimpleNamespace(guilds=[guild]), service, translator, notes=notes)
    return asyncio.run(handler.post_pending())


def marker(service, guild_id=1):
    return service.settings[guild_id].update_news_last_id


def test_a_server_new_to_update_news_gets_the_newest_note_in_its_general_channel():
    general = FakeChannel(10)
    service = FakeSettingsService()

    assert post(FakeGuild(system_channel=general), service) == 1

    (embeds,) = general.sent
    (embed,) = embeds
    assert embed.title == "📰 Title 5" and embed.description == "Text 5 with `/settings`"
    assert embed.footer.text == NEWS_FOOTER
    assert marker(service) == "note-5"
    # The next start has nothing more to say.
    assert post(FakeGuild(system_channel=general), service) == 0 and len(general.sent) == 1


def test_a_server_that_fell_behind_gets_the_newest_notes_oldest_first():
    general = FakeChannel(10)
    service = FakeSettingsService(update_news_last_id="note-1")

    assert post(FakeGuild(system_channel=general), service) == 3

    assert [embeds[0].title for embeds in general.sent] == ["📰 Title 3", "📰 Title 4", "📰 Title 5"]
    assert marker(service) == "note-5"


def test_a_chosen_channel_wins_and_is_never_replaced_by_the_general_one():
    general, news = FakeChannel(10), FakeChannel(20, "news")
    service = FakeSettingsService(update_news_last_id="note-4", update_news_channel_id=20)

    assert post(FakeGuild(system_channel=general, text_channels=[news]), service) == 1
    assert len(news.sent) == 1 and general.sent == []

    # The chosen channel was deleted: the note waits, it does not go to general.
    service = FakeSettingsService(update_news_last_id="note-4", update_news_channel_id=99)
    assert post(FakeGuild(system_channel=general), service) == 0
    assert general.sent == [] and marker(service) == "note-4"


def test_without_a_system_channel_a_channel_called_general_is_used_or_nothing():
    chat, named = FakeChannel(30, "chat"), FakeChannel(31, "General")
    service = FakeSettingsService(update_news_last_id="note-4")

    assert post(FakeGuild(text_channels=[chat, named]), service) == 1
    assert len(named.sent) == 1 and chat.sent == []

    # No general channel, or one the bot cannot write in: nothing is posted anywhere,
    # and the note is kept for a start that finds a channel.
    for guild in (FakeGuild(text_channels=[chat]), FakeGuild(system_channel=FakeChannel(10, closed=True))):
        service = FakeSettingsService(update_news_last_id="note-4")
        assert general_channel(guild) is None
        assert post(guild, service) == 0
        assert chat.sent == [] and marker(service) == "note-4"


def test_a_server_that_switched_news_off_is_caught_up_silently():
    general = FakeChannel(10)
    service = FakeSettingsService(update_news_last_id="note-2", update_news_enabled=False)

    assert post(FakeGuild(system_channel=general), service) == 0
    assert general.sent == [] and marker(service) == "note-5"

    # Switched back on later: it starts from now, without the notes it skipped.
    service.settings[1].update_news_enabled = True
    assert post(FakeGuild(system_channel=general), service) == 0


def test_a_withdrawn_marker_posts_nothing_and_starts_over_from_the_newest_note():
    general = FakeChannel(10)
    service = FakeSettingsService(update_news_last_id="withdrawn")

    assert post(FakeGuild(system_channel=general), service) == 0
    assert general.sent == [] and marker(service) == "note-5"


def test_news_is_posted_in_the_servers_languages_in_one_message():
    general = FakeChannel(10)
    service = FakeSettingsService(update_news_last_id="note-4", translation_languages="uk,en,de")
    translator = FakeTranslator()

    assert post(FakeGuild(system_channel=general), service, translator=translator) == 1

    (embeds,) = general.sent
    assert [embed.title for embed in embeds] == ["🇬🇧 Title 5", "🇺🇦 <Ukrainian> Title 5", "🇩🇪 <German> Title 5"]
    assert embeds[1].description == "<Ukrainian> Text 5 with `/settings`"
    # One footer, under the last language, in that language.
    assert [embed.footer.text for embed in embeds] == [None, None, f"<German> {NEWS_FOOTER}"]
    assert "`" not in NEWS_FOOTER and NEWS_FOOTER.endswith("in /settings")
    # English is never sent to the model.
    assert {language for _, language in translator.calls} == {"Ukrainian", "German"}


def test_translations_are_made_once_for_all_servers():
    channels = [FakeChannel(10), FakeChannel(11)]
    guilds = [FakeGuild(1, system_channel=channels[0]), FakeGuild(2, system_channel=channels[1])]
    service = FakeSettingsService(update_news_last_id="note-4", translation_languages="uk")
    translator = FakeTranslator()
    handler = UpdateNewsHandler(SimpleNamespace(guilds=guilds), service, translator, notes=NOTES)

    assert asyncio.run(handler.post_pending()) == 2
    assert len(translator.calls) == 3  # title, text and footer, not twice
    assert all(channel.sent[0][0].title == "📰 <Ukrainian> Title 5" for channel in channels)


def test_a_translation_that_fails_or_breaks_a_command_name_falls_back_to_english_once():
    for translator in (FakeTranslator(broken={"Ukrainian"}), FakeTranslator(drop_code={"Ukrainian"}), None):
        general = FakeChannel(10)
        service = FakeSettingsService(update_news_last_id="note-4", translation_languages="en,uk")

        assert post(FakeGuild(system_channel=general), service, translator=translator) == 1

        (embeds,) = general.sent
        # Not the same English text twice.
        assert [embed.description for embed in embeds] == ["Text 5 with `/settings`"]
        assert embeds[0].title == "📰 Title 5" and embeds[0].footer.text == NEWS_FOOTER

    assert keeps_code_spans("Run `/settings` now", "Запустіть `/settings` зараз")
    assert not keeps_code_spans("Run `/settings` now", "Запустіть `/налаштування` зараз")


def test_long_translations_are_split_into_several_messages():
    long_notes = (UpdateNote("long", "Title", "x" * 900),)
    general = FakeChannel(10)
    service = FakeSettingsService(translation_languages="en,uk,de")

    class Wordy:
        async def translate_to_language(self, text, language):
            return {"text": f"{language}: " + text * 3}

    assert post(FakeGuild(system_channel=general), service, translator=Wordy(), notes=long_notes) == 1

    assert len(general.sent) == 2
    assert all(sum(len(embed) for embed in embeds) <= 6000 for embeds in general.sent)
    assert sum(len(embeds) for embeds in general.sent) == 3


def test_notes_that_could_not_be_posted_are_given_back_for_the_next_start():
    # The second of three messages fails: the first note counts as told.
    general = FakeChannel(10, fail=2)
    service = FakeSettingsService(update_news_last_id="note-2")

    assert post(FakeGuild(system_channel=general), service) == 1
    assert marker(service) == "note-3"

    general = FakeChannel(10, fail=1)
    service = FakeSettingsService()
    assert post(FakeGuild(system_channel=general), service) == 0
    assert marker(service) is None


def test_a_note_another_instance_already_claimed_is_not_posted_again():
    general = FakeChannel(10)
    service = FakeSettingsService(update_news_last_id="note-4")
    service.lose_claims = True

    assert post(FakeGuild(system_channel=general), service) == 0
    assert general.sent == []


def test_one_servers_failure_does_not_stop_the_others():
    good = FakeChannel(11)
    service = FakeSettingsService(update_news_last_id="note-4")

    class BrokenGuild(FakeGuild):
        @property
        def system_channel(self):
            raise RuntimeError("cache miss")

        @system_channel.setter
        def system_channel(self, value):
            pass

    handler = UpdateNewsHandler(
        SimpleNamespace(guilds=[BrokenGuild(1), FakeGuild(2, system_channel=good)]), service, notes=NOTES
    )

    assert asyncio.run(handler.post_pending()) == 1 and len(good.sent) == 1


def test_a_server_that_just_added_the_bot_is_not_told_the_past():
    general = FakeChannel(10)
    service = FakeSettingsService()
    handler = UpdateNewsHandler(SimpleNamespace(guilds=[FakeGuild(system_channel=general)]), service, notes=NOTES)

    async def run_test():
        await handler.mark_seen(1)
        return await handler.post_pending()

    assert asyncio.run(run_test()) == 0
    assert general.sent == [] and marker(service) == "note-5"


def test_news_starts_once_however_often_the_bot_reconnects():
    general = FakeChannel(10)
    service = FakeSettingsService()
    handler = UpdateNewsHandler(SimpleNamespace(guilds=[FakeGuild(system_channel=general)]), service, notes=NOTES)

    async def run_test():
        handler.start()
        first = handler._task
        handler.start()
        await first
        return handler._task is first

    assert asyncio.run(run_test()) is True and len(general.sent) == 1


def test_no_notes_means_nothing_is_read_or_posted():
    handler = UpdateNewsHandler(SimpleNamespace(guilds=[object()]), None, notes=())

    assert asyncio.run(handler.post_pending()) == 0


def test_settings_say_where_the_news_goes():
    general = FakeChannel(10)
    guild = FakeGuild(system_channel=general)
    settings = lambda **values: SimpleNamespace(**values)  # noqa: E731

    assert describe_destination(guild, settings()) == "<#10> · the general channel"
    assert describe_destination(guild, settings(update_news_channel_id=20)) == "<#20>"
    assert describe_destination(guild, settings(update_news_enabled=False, update_news_channel_id=20)) == "**Off**"
    assert describe_destination(FakeGuild(), settings()).startswith("no general channel I can post in")
