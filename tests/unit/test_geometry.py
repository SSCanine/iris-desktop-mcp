from iris.geometry import Rect


def test_rect_from_ltrb():
    r = Rect.from_ltrb(10, 20, 30, 40)
    assert r.x == 10
    assert r.y == 20
    assert r.width == 20
    assert r.height == 20


def test_rect_intersects():
    a = Rect(0, 0, 100, 100)
    b = Rect(50, 50, 100, 100)
    assert a.intersects(b)


def test_rect_does_not_intersect():
    a = Rect(0, 0, 50, 50)
    b = Rect(100, 100, 50, 50)
    assert not a.intersects(b)


def test_rect_intersection_area():
    a = Rect(0, 0, 100, 100)
    b = Rect(50, 50, 100, 100)
    assert a.intersection_area(b) == 50 * 50


def test_rect_intersection_area_zero_when_disjoint():
    a = Rect(0, 0, 50, 50)
    b = Rect(100, 100, 50, 50)
    assert a.intersection_area(b) == 0


def test_rect_center():
    r = Rect(0, 0, 100, 200)
    assert r.center == (50, 100)


def test_rect_to_dict():
    r = Rect(10, 20, 30, 40)
    assert r.to_dict() == {"x": 10, "y": 20, "width": 30, "height": 40}


def test_rect_contains_point():
    r = Rect(10, 20, 30, 40)
    assert r.contains_point(15, 25)
    assert not r.contains_point(5, 25)
    assert not r.contains_point(40, 25)


def test_rect_shift():
    r = Rect(10, 20, 30, 40)
    s = r.shift(5, 7)
    assert s == Rect(15, 27, 30, 40)
