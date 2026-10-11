"""Tests for admin user directory aggregation and pagination without PostgreSQL."""
import database


def test_get_admin_users_search_pagination_and_online_summary(monkeypatch):
    calls=[]
    class Cursor:
        def __enter__(self): return self
        def __exit__(self,*args): return False
        def execute(self,q,p=None):
            calls.append((q,p))
            self.result={"total":3,"online":1} if "SELECT COUNT(*) AS total" in q else [{"telegram_id":12345,"is_online":True,"owned_cards":4,"duplicates":2,"wanted_cards":1}]
        def fetchone(self): return self.result
        def fetchall(self): return self.result
    class Conn:
        def __enter__(self): return self
        def __exit__(self,*args): return False
        def cursor(self): return Cursor()
    monkeypatch.setattr(database,"get_connection",lambda:Conn())
    result=database.get_admin_users(search=" player ",online_seconds=90,limit=10,offset=20)
    assert (result["total"],result["online"],result["limit"],result["offset"])==(3,1,10,20)
    assert result["users"][0]["telegram_id"]==12345
    assert calls[0][1]==(90,"player","%player%","%player%","%player%","%player%")
    assert calls[1][1]==(90,"player","%player%","%player%","%player%","%player%",10,20)
    assert "SUM(GREATEST(quantity - 1, 0))" in calls[1][0]


def test_get_admin_users_clamps_pagination_and_online_window(monkeypatch):
    calls=[]
    class Cursor:
        def __enter__(self): return self
        def __exit__(self,*args): return False
        def execute(self,q,p=None): calls.append((q,p))
        def fetchone(self): return {"total":0,"online":0}
        def fetchall(self): return []
    class Conn:
        def __enter__(self): return self
        def __exit__(self,*args): return False
        def cursor(self): return Cursor()
    monkeypatch.setattr(database,"get_connection",lambda:Conn())
    result=database.get_admin_users(online_seconds=1,limit=1000,offset=-5)
    assert (result["online_seconds"],result["limit"],result["offset"])==(30,100,0)
    assert calls[1][1][-2:]==(100,0)


def test_get_admin_user_returns_none_when_not_found(monkeypatch):
    class Cursor:
        def __enter__(self): return self
        def __exit__(self,*args): return False
        def execute(self,*args): pass
        def fetchone(self): return None
    class Conn:
        def __enter__(self): return self
        def __exit__(self,*args): return False
        def cursor(self): return Cursor()
    monkeypatch.setattr(database,"get_connection",lambda:Conn())
    assert database.get_admin_user(999999) is None
