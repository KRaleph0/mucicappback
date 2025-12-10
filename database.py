# database.py
import oracledb
from flask import g
import config

db_pool = None

def init_db_pool():
    """앱 시작 시 DB 풀 생성"""
    global db_pool
    try:
        db_pool = oracledb.create_pool(
            user=config.DB_USER,
            password=config.DB_PASSWORD,
            dsn=config.DB_DSN,
            min=1, max=5
        )
        print("[DB] Oracle Pool 생성 완료.")
    except Exception as e:
        print(f"[DB 오류] {e}")
        db_pool = None

def get_db_connection():
    """요청 시 커넥션 가져오기"""
    if not db_pool: raise Exception("DB 풀 없음")
    if 'db' not in g: g.db = db_pool.acquire()
    return g.db

def close_db(exception=None):
    db = g.pop('db', None)
    if db is not None:
        try:
            db.close()
        except oracledb.exceptions.InterfaceError as e:
            # 이미 끊어진 연결(DPY-1001)은 그냥 무시
            # 다른 에러 코드면 필요시 로깅 정도만
            print(f"[DB Close Warning] {e}")
        except Exception as e:
            # teardown에서 여기서 또 예외 터지면 응답이 500으로 날아가니 그냥 로그만 남기고 무시
            print(f"[DB Close Error] {e}")