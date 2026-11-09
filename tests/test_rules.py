from app.engine import increment_for

def test_increment_tiers():
    assert increment_for(0) == 10
    assert increment_for(199) == 10
    assert increment_for(200) == 20
    assert increment_for(499) == 20
    assert increment_for(500) == 25
