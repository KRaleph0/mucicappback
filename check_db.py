import oracledb
import config

def check_tag_data(target_tag):
    print(f"\n🔍 [DB 진단 시작] 검색어: '{target_tag}' 확인 중...")

    conn = None  # [핵심] 이 줄이 있어야 에러가 안 납니다!
    try:
        # Flask 의존성 없이 직접 연결
        conn = oracledb.connect(
            user=config.DB_USER,
            password=config.DB_PASSWORD,
            dsn=config.DB_DSN
        )
        cur = conn.cursor()

        print("\n1️⃣ TRACK_TAGS 테이블 조회 결과:")
        # 대소문자 무시 검색 (LOWER)
        cur.execute("""
            SELECT tag_id, COUNT(*) 
            FROM TRACK_TAGS 
            WHERE LOWER(tag_id) LIKE LOWER(:tag)
            GROUP BY tag_id
        """, [f"%{target_tag}%"])

        tags = cur.fetchall()
        if not tags:
            print("   ❌ 해당 태그 데이터가 아예 없습니다.")
        else:
            for t in tags:
                print(f"   ✅ 발견됨: '{t[0]}' (연결된 곡: {t[1]}개)")

    except Exception as e:
        print(f"❌ DB 오류 발생: {e}")
    finally:
        if conn: conn.close()

if __name__ == "__main__":
    check_tag_data("tag:jpop")
