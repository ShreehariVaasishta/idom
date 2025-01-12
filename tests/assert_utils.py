def assert_same_items(left, right):
    """Check that two unordered sequences are equal (only works if reprs are equal)"""
    sorted_left = list(sorted(left, key=repr))
    sorted_right = list(sorted(right, key=repr))
    assert sorted_left == sorted_right
