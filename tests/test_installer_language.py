"""Installer UI language: Russian Windows gets Russian, everyone else English."""
import install


def test_russian_locales_get_the_russian_installer():
    assert install.installer_language("ru-RU") == "ru"
    assert install.installer_language("ru") == "ru"
    assert install.installer_language("ru_RU") == "ru"


def test_other_locales_fall_back_to_english():
    for locale in ("en-US", "de-DE", "uk-UA", "ja-JP", "", "  "):
        assert install.installer_language(locale) == "en"


def test_texts_cover_both_languages():
    en = install.INSTALLER_TEXTS["en"]
    ru = install.INSTALLER_TEXTS["ru"]
    assert set(en) == set(ru)
    assert "VibeForma" in en["title"] and "VibeForma" in ru["title"]


def test_russian_strings_are_russian():
    assert install.installer_text("ru", "install") == "Установить"
    assert "панель мониторинга" in install.installer_text("ru", "done")
    assert "Ctrl+Alt+Space" in install.installer_text("ru", "done")


def test_unknown_key_and_language_fall_back_to_english():
    assert install.installer_text("de", "install") == "Install"
    assert install.installer_text("ru", "no_such_key") == "no_such_key"


def test_model_description_follows_the_ui_language():
    ru = install.model_description("small", "ru")
    en = install.model_description("small", "en")
    assert "ГБ" in ru
    assert "GB" in en
    assert "small" in ru and "small" in en
