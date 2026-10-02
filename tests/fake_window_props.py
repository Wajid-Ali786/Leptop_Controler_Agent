"""The ONE offline stand-in for the window-ownership property boundary.

app/executor/adapter.py marks the windows the assistant opened with a Windows window property
(SetPropW/GetPropW/RemovePropW). Those three functions are real cross-process Win32 calls, so the
repository-root guard refuses them in every offline test; a fixture with its own fake desktop installs
this in their place.

It is deliberately the only implementation, so no test can accidentally model the semantics that make
the ownership token work differently from the production adapter:

  * a property belongs to the window OBJECT, so `exists` decides whether one can be read or written at
    all, and a test that destroys a window drops its entry from the returned dict
  * tag_window does SetPropW AND the GetPropW read-back, reporting success only on an exact match
  * untag_window removes the property only when it currently holds exactly our token, because Windows
    documents that an application must not remove properties added by anyone else
  * `refuse` stands for a window SetPropW cannot touch - the documented UIPI case, where the target
    belongs to a process of higher integrity level
"""
TOKEN_KEY = "ownership"  # this fake's key; the real one is internal to app/executor/adapter.py


def install(monkeypatch, adapter, *, exists=None, refuse=()):
    """Replace the adapter's three property functions with an in-memory property store, and return it
    as {handle: {key: value}} so a fake desktop can drop a destroyed window's entry."""
    props: dict[int, dict] = {}

    def present(handle):
        return True if exists is None else bool(exists(handle))

    def tag_window(handle, token):
        if handle in refuse or not present(handle):
            return False
        props.setdefault(handle, {})[TOKEN_KEY] = token
        return props[handle].get(TOKEN_KEY) == token      # the adapter's read-back

    def window_token(handle):
        return props.get(handle, {}).get(TOKEN_KEY) if present(handle) else None

    def untag_window(handle, token):
        if window_token(handle) != token:
            return False
        return props.get(handle, {}).pop(TOKEN_KEY, None) == token

    monkeypatch.setattr(adapter, "tag_window", tag_window)
    monkeypatch.setattr(adapter, "window_token", window_token)
    monkeypatch.setattr(adapter, "untag_window", untag_window)
    return props
