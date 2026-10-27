from pillow_assistant.core.session import Session


def test_an_empty_session_still_counts_as_a_session():
    session = Session("p1")  # bound to a project, no chips
    assert len(session) == 0 and bool(session)
