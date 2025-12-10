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

def close_db(e=None):
    """요청 종료 시 커넥션 반납"""
    db = g.pop('db', None)
    if db: db.close()