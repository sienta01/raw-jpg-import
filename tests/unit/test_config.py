from pathlib import PurePosixPath

import pytest

from raw_jpg_import import appdirs
from raw_jpg_import.config import load_config, template_text, write_template
from raw_jpg_import.errors import ConfigError


def _write(tmp_path, text):
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_defaults_are_unique_lowercase_dotless():
    t = load_config(use_user=False).formats
    seen = set()
    for cat in t.categories:
        for e in cat.extensions:
            assert e == e.lower() and "." not in e and e
            assert e not in seen, e
            seen.add(e)


def test_default_folders_and_heif_in_jpg():
    t = load_config(use_user=False).formats
    assert t.classify("DSC_0001.NEF").folder == "RAW"
    assert t.classify("DSC_0001.JPG").folder == "JPG"
    assert t.classify("DSC_0001.HIF").folder == "JPG"
    assert t.classify("DSC_0001.MOV").folder == "MOV"
    assert t.classify("DSC_0001.MP4").folder == "MOV"
    assert t.classify("DSC_0001.WAV").role == "sidecar"
    assert t.classify("NIKON001.DSC").role == "ignore"
    assert t.classify("file.xyz") is None
    assert t.folders() == ["RAW", "JPG", "MOV"]


def test_missing_user_config_is_fine(tmp_path):
    cfg = load_config(tmp_path / "nope.toml")
    assert cfg.user_path is None and cfg.warnings == []


def test_replace_add_remove(tmp_path):
    p = _write(tmp_path, """
schema = 1
[categories.raw]
add = ["cri"]
remove = ["dng"]
[categories.jpg]
extensions = ["jpg"]
""")
    t = load_config(p).formats
    assert t.classify("a.cri").key == "raw"
    assert t.classify("a.dng") is None
    assert t.classify("a.jpeg") is None
    assert t.classify("a.JPG").key == "jpg"


def test_new_category_claims_from_default(tmp_path):
    p = _write(tmp_path, """
[categories.proxy]
folder = "PROXY"
role = "primary"
rank = 4
extensions = ["lrv"]
""")
    t = load_config(p).formats
    assert t.classify("GL010001.LRV").key == "proxy"
    assert "lrv" not in t.category("ignore").extensions
    assert t.folders() == ["RAW", "JPG", "MOV", "PROXY"]


def test_heif_can_get_own_folder(tmp_path):
    p = _write(tmp_path, '[categories.heif]\nfolder = "HEIF"\n')
    assert load_config(p).formats.classify("x.hif").folder == "HEIF"


def test_two_user_claims_error_names_both(tmp_path):
    p = _write(tmp_path, """
[categories.a]
folder = "A"
extensions = ["zzz"]
[categories.b]
folder = "B"
extensions = ["zzz"]
""")
    with pytest.raises(ConfigError) as e:
        load_config(p)
    assert "categories.a" in str(e.value) and "categories.b" in str(e.value)


@pytest.mark.parametrize("folder", ["RA:W", "", "a/b", "CON"])
def test_bad_folder_rejected(tmp_path, folder):
    p = _write(tmp_path, f'[categories.raw]\nfolder = "{folder}"\n')
    with pytest.raises(ConfigError):
        load_config(p)


def test_bad_role_and_rank(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, '[categories.raw]\nrole = "keep"\n'))
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, '[categories.raw]\nrank = "x"\n'))


def test_toml_syntax_error_has_line_and_col(tmp_path):
    p = _write(tmp_path, "schema = 1\n[categories.raw\nfolder = 'x'\n")
    with pytest.raises(ConfigError) as e:
        load_config(p)
    assert e.value.line == 2


def test_unknown_keys_warn(tmp_path):
    p = _write(tmp_path, "colour = 1\n[categories.raw]\nfoldr = 'RAW'\n[scan]\nnope = []\n")
    cfg = load_config(p)
    assert len(cfg.warnings) == 3


def test_schema_mismatch(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "schema = 2\n"))


def test_unknown_policy(tmp_path):
    assert load_config(_write(tmp_path, '[import]\nunknown = "copy"\n')).formats.unknown_policy \
        == "copy"
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, '[import]\nunknown = "maybe"\n'))


def test_template_is_inert_and_init_refuses_overwrite(tmp_path):
    p = tmp_path / "cfg" / "config.toml"
    write_template(p)
    cfg = load_config(p)
    assert cfg.formats == load_config(use_user=False).formats
    assert cfg.warnings == []
    with pytest.raises(FileExistsError):
        write_template(p)
    assert "schema = 1" in template_text()


def test_default_config_path_uses_home(isolated_home):
    assert appdirs.config_file() == isolated_home / "config.toml"
    write_template()
    assert load_config().user_path == isolated_home / "config.toml"


def test_skip_rules(fmt):
    rel = PurePosixPath
    assert fmt.skip_dir("$RECYCLE.BIN", rel("$RECYCLE.BIN"))
    assert fmt.skip_dir("System Volume Information", rel("System Volume Information"))
    assert fmt.skip_dir(".Trashes", rel(".Trashes"))
    assert fmt.skip_dir(".Trash-1000", rel(".Trash-1000"))
    assert fmt.skip_dir("THMBNL", rel("PRIVATE/M4ROOT/THMBNL"))
    assert not fmt.skip_dir("CLIP", rel("PRIVATE/M4ROOT/CLIP"))
    assert not fmt.skip_dir("100NCZ_F", rel("DCIM/100NCZ_F"))
    assert fmt.skip_file("._DSC_0001.NEF", rel("DCIM/100NCZ_F/._DSC_0001.NEF"))
    assert fmt.skip_file("NC_FLLST.DAT", rel("NC_FLLST.DAT"))
    assert fmt.skip_file(".DSC_0001.NEF.rji-abc123.part", rel("x/.DSC_0001.NEF.rji-abc123.part"))
    assert fmt.skip_file("Thumbs.db", rel("DCIM/Thumbs.db"))
    assert not fmt.skip_file("DSC_0001.NEF", rel("DCIM/100NCZ_F/DSC_0001.NEF"))


def test_classify_is_case_insensitive(fmt):
    assert fmt.classify("dsc_0001.nef").key == "raw"
    assert fmt.classify("DSC_0001.Nef").key == "raw"
    assert fmt.classify("DSC_0001.NEF.xmp").role == "sidecar"
    assert fmt.classify("README") is None
