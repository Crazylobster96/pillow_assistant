"""present_windows finds workspace files from relative or wrongly guessed absolute paths."""

from pillow_assistant.core.tools.builtin.present_tool import locate


def test_locate_finds_workspace_files(tmp_path):
    shot = tmp_path / "tmp" / "render" / "top.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(b"png")
    assert locate("tmp/render/top.png", tmp_path) == shot
    assert locate("/Users/someone/Downloads/plate/tmp/render/top.png", tmp_path) == shot
    assert locate(str(shot), tmp_path) == shot
    assert not locate("/Users/someone/elsewhere/none.png", tmp_path).exists()
