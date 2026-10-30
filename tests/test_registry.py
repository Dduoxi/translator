import pytest

from fpst.core.interfaces import Translator
from fpst.core.registry import RegistryError, available, create
from fpst.core.types import Lang
from fpst.translate.mock import MockTranslator  # noqa: F401 - registers "mock"


def test_create_known_engine():
    tr = create(Translator, "mock")
    assert isinstance(tr, MockTranslator)
    assert "mock" in available(Translator)


def test_create_unknown_engine_lists_options():
    with pytest.raises(RegistryError) as exc:
        create(Translator, "does-not-exist")
    # The message must tell the user what *is* available.
    assert "available:" in str(exc.value)
    assert "mock" in str(exc.value)


def test_unknown_kind():
    class NotRegistered:
        pass

    with pytest.raises(RegistryError):
        create(NotRegistered, "x")


def test_translator_batch_contract():
    tr = create(Translator, "mock", glossary={"rush": "快攻"})
    out = tr.translate(["rush", "hold the line", "  "], Lang.EN, Lang.ZH)
    assert out == ["快攻", "⟦zh⟧hold the line", ""]


def test_translate_one_convenience():
    tr = create(Translator, "mock", glossary={"gg": "打得好"})
    assert tr.translate_one("gg", Lang.EN, Lang.ZH) == "打得好"
