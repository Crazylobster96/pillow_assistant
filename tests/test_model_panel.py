"""STL and STEP open in a 3D window Pillow draws itself."""

from pillow_assistant.ui.panels.model_panel import mesh_for, viewer_html
from pillow_assistant.ui.viewer_registry import classify


def test_models_open_in_the_3d_panel(tmp_path):
    for name in ("a.stl", "b.STEP", "c.stp"):
        assert classify(tmp_path / name) == "model"


def test_a_step_is_drawn_from_the_stl_of_the_same_part(tmp_path):
    (tmp_path / "STEP").mkdir()
    (tmp_path / "STL").mkdir()
    step = tmp_path / "STEP" / "plate.step"
    step.write_text("ISO-10303-21;", "utf-8")
    assert mesh_for(step) is None
    review = tmp_path / ".pillow" / "review"
    review.mkdir(parents=True)
    (review / "plate.stl").write_bytes(b"solid x\nendsolid x\n")
    assert mesh_for(step) == review / "plate.stl"  # the one the reviewer made
    (tmp_path / "STL" / "plate.stl").write_bytes(b"solid x\nendsolid x\n")
    assert mesh_for(step) == tmp_path / "STL" / "plate.stl"  # the exported one first


def test_the_viewer_page_carries_the_mesh_and_needs_nothing_from_the_net():
    page = viewer_html(b"solid x\nendsolid x\n", "plate.stl")
    assert "c29saWQge" in page and "<script src" not in page and "http" not in page.split("<script>")[1]


def test_a_step_without_a_mesh_is_tessellated_into_its_project(tmp_path, monkeypatch):
    from pillow_assistant.ui.panels.model_panel import converted_stl

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    (tmp_path / "ws" / ".pillow").mkdir(parents=True)
    (tmp_path / "ws" / "STEP").mkdir()
    step = tmp_path / "ws" / "STEP" / "plate.step"
    step.write_text("ISO-10303-21;", "utf-8")
    assert converted_stl(step) == tmp_path / "ws" / ".pillow" / "review" / "plate.stl"
    loose = tmp_path / "home" / "Downloads" / "loose.step"  # ~/.pillow is not a project
    (tmp_path / "home" / ".pillow").mkdir(parents=True)
    assert converted_stl(loose).parent == tmp_path / "home" / ".pillow" / "cache" / "model_view"
