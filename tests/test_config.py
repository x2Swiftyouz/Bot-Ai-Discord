import pytest

from bot.config import Config, ConfigError


def test_minimal_config(env):
    cfg = Config.load()
    assert cfg.provider == "gemini"
    assert cfg.api_keys == ("gemini-key",)
    assert cfg.backups == ()


def test_api_key_in_fallback_models_becomes_extra_key(env):
    key = "gsk_" + "k" * 40
    env(GROQ_API_KEY="groq-key", GROQ_MODEL="gpt-oss", GROQ_FALLBACK_MODELS=f"{key},gpt-small",
        BACKUP_PROVIDER="groq")
    groq = Config.load().backups[0]
    assert groq.api_keys == ("groq-key", key)
    assert groq.fallback_models == ("gpt-small",)


def test_auto_backup_uses_every_configured_provider(env):
    env(BACKUP_PROVIDER="", GROQ_API_KEY="g", GROQ_MODEL="q",
        OPENROUTER_API_KEY="o", OPENROUTER_MODEL="x:free", OPENROUTER_VISION_MODEL="v:free")
    cfg = Config.load()
    assert [b.provider for b in cfg.backups] == ["groq", "openrouter"]
    assert cfg.backups[1].vision_model == "v:free"


@pytest.mark.parametrize(
    "extra",
    [{"BACKUP_PROVIDER": "gemini"}, {"BACKUP_PROVIDER": "foo"}, {"BACKUP_PROVIDER": "openrouter"},
     {"TIMEZONE": "Mars/Base"}, {"GEMINI_MODEL": "gsk_" + "m" * 40}],
)
def test_invalid_config(env, extra):
    env(**extra)
    with pytest.raises(ConfigError):
        Config.load()
