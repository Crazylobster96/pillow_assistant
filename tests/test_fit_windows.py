"""Display windows open at the size their content needs, centred, within their share of the screen."""

from pillow_assistant.ui.event_presenter import fit_windows

AREA = (0, 0, 2560, 1400)


def test_one_small_window_is_not_blown_up_to_the_screen():
    (box,), _ = fit_windows([(920, 700)], AREA)
    x, y, w, h = box
    assert (w, h) == (920, 700)
    assert abs((x + w / 2) - 1280) <= 1  # centred


def test_big_content_is_capped_to_its_share():
    boxes, _ = fit_windows([(3000, 3000), (600, 400)], AREA)
    assert boxes[0][2] <= (2560 - 8) // 2 and boxes[0][3] <= int(1400 * 0.72)
    assert boxes[1][2] == 600 and boxes[1][3] == boxes[0][3]  # side by side: one height
    assert boxes[1][0] == boxes[0][0] + boxes[0][2] + 8


def test_two_files_side_by_side_share_one_height():
    boxes, _ = fit_windows([(920, 820), (700, 500)], AREA)
    assert boxes[0][3] == boxes[1][3] == 820 and boxes[0][1] == boxes[1][1]
    assert (boxes[0][2], boxes[1][2]) == (920, 700)  # widths stay their own


def test_stacked_windows_keep_their_own_heights():
    boxes, bar_y = fit_windows([(700, 300), (600, 500)], AREA, "column")
    assert [b[3] for b in boxes] == [300, 500] and boxes[1][1] == boxes[0][1] + 300 + 8
    assert boxes[0][2] == boxes[1][2] == 700  # stacked: one width
    assert bar_y > boxes[1][1] + 500
