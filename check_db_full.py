import oracledb
import config

def check_full_query(target_tag):
    print(f"\n🔍 [정밀 진단] 'app.py'와 동일한 쿼리로 '{target_tag}' 검색 테스트 중...")
    
    conn = None
    try:
        conn = oracledb.connect(
            user=config.DB_USER,
            password=config.DB_PASSWORD,
            dsn=config.DB_DSN
        )
        cur = conn.cursor()

        # [중요] app.py와 100% 동일한 쿼리
        sql = """
            SELECT t.track_id, t.track_title, t.artist_name, t.image_url, t.preview_url, a.album_title
            FROM TRACKS t 
            JOIN TRACK_TAGS tt ON t.track_id = tt.track_id
            LEFT JOIN ALBUMS a ON t.album_id = a.album_id
            WHERE LOWER(tt.tag_id) = LOWER(:tag)
            ORDER BY t.views DESC
        """
        
        print("\n⏳ 쿼리 실행 중...")
        cur.execute(sql, [target_tag.strip()])
        
        rows = cur.fetchall()
        if rows:
            print(f"   ✅ 성공! {len(rows)}개의 데이터가 조회되었습니다.")
            for r in rows:
                print(f"      - {r[1]} (Artist: {r[2]})")
        else:
            print("   ⚠️ 쿼리는 실행됐지만 결과가 0건입니다.")
            print("      (데이터는 있는데 결과가 없다면, JOIN 조건이나 WHERE 절 문제일 수 있습니다.)")

    except oracledb.DatabaseError as e:
        error, = e.args
        print(f"\n❌ [치명적 오류] 쿼리 실행 실패!")
        print(f"   오류 코드: {error.code}")
        print(f"   오류 메시지: {error.message}")
        print("\n💡 [힌트]")
        if "invalid identifier" in error.message:
            if "VIEWS" in error.message:
                print("   👉 'TRACKS' 테이블에 'views'라는 컬럼이 없는 것 같습니다.")
            elif "ALBUM_ID" in error.message:
                print("   👉 'TRACKS' 테이블에 'album_id' 컬럼이 없거나 'ALBUMS' 테이블이 없습니다.")
        elif "table or view does not exist" in error.message:
            print("   👉 쿼리에 사용된 테이블(ALBUMS 등) 중 하나가 DB에 없습니다.")

    except Exception as e:
        print(f"❌ 알 수 없는 오류: {e}")
    finally:
        if conn: conn.close()

if __name__ == "__main__":
    check_full_query("tag:jpop")