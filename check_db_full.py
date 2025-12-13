import oracledb
import config

def check_full_query(target_tag):
    print(f"\n🔍 [재검증] 수정된 로직(ALBUMS 제외)으로 '{target_tag}' 검색 테스트 중...")
    
    conn = None
    try:
        # DB 직접 연결
        conn = oracledb.connect(
            user=config.DB_USER,
            password=config.DB_PASSWORD,
            dsn=config.DB_DSN
        )
        cur = conn.cursor()

        # [검증할 쿼리] app.py에 적용한 것과 동일 (ALBUMS 테이블 JOIN 제거됨)
        sql = """
            SELECT t.track_id, t.track_title, t.artist_name, t.image_url, t.preview_url
            FROM TRACKS t 
            JOIN TRACK_TAGS tt ON t.track_id = tt.track_id
            WHERE LOWER(tt.tag_id) = LOWER(:tag)
            ORDER BY t.views DESC
        """
        
        print("\n⏳ 쿼리 실행 중...")
        cur.execute(sql, [target_tag.strip()])
        
        rows = cur.fetchall()
        if rows:
            print(f"   ✅ 성공! {len(rows)}개의 데이터가 정상 조회되었습니다.")
            for r in rows:
                print(f"      🎵 {r[1]} (Artist: {r[2]})")
        else:
            print("   ⚠️ 쿼리 오류는 없지만, 결과가 0건입니다.")

    except oracledb.DatabaseError as e:
        error, = e.args
        print(f"\n❌ [오류 발생] 여전히 문제가 있습니다.")
        print(f"   오류 메시지: {error.message}")
        if "invalid identifier" in error.message and "VIEWS" in error.message:
             print("   👉 원인: TRACKS 테이블에 'views' 컬럼도 없는 것 같습니다.")

    except Exception as e:
        print(f"❌ 알 수 없는 오류: {e}")
    finally:
        if conn: conn.close()

if __name__ == "__main__":
    check_full_query("tag:jpop")