import oracledb
import config

def repair_database():
    print("🔧 [DB Repair] 데이터베이스 점검 및 복구를 시작합니다...")
    
    try:
        conn = oracledb.connect(
            user=config.DB_USER,
            password=config.DB_PASSWORD,
            dsn=config.DB_DSN
        )
        cur = conn.cursor()

        # 1. USERS 테이블에 is_banned 컬럼이 있는지 확인하고 없으면 추가
        try:
            print("   -> 'is_banned' 컬럼 점검 중...")
            cur.execute("SELECT is_banned FROM USERS FETCH FIRST 1 ROWS ONLY")
        except oracledb.DatabaseError as e:
            if "ORA-00904" in str(e): # invalid identifier (컬럼 없음)
                print("   -> ⚠️ 컬럼이 없습니다. 'is_banned' 컬럼을 추가합니다.")
                cur.execute("ALTER TABLE USERS ADD (is_banned NUMBER(1) DEFAULT 0)")
            else:
                print(f"   -> ❌ 점검 중 에러: {e}")

        # 2. is_banned 값이 NULL인 유저들을 0(정상)으로 일괄 업데이트
        print("   -> NULL 데이터 일괄 복구 중 (NULL -> 0)...")
        cur.execute("UPDATE USERS SET is_banned = 0 WHERE is_banned IS NULL")
        updated_rows = cur.rowcount
        print(f"   -> ✅ {updated_rows}명의 유저 정보를 복구했습니다.")

        conn.commit()
        cur.close()
        conn.close()
        print("\n✨ [완료] DB 복구가 끝났습니다. 이제 태그 수정이 가능합니다!")

    except Exception as e:
        print(f"\n❌ [치명적 에러] 연결 실패: {e}")

if __name__ == "__main__":
    repair_database()