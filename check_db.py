from database import get_db_connection

def check_tag_data(target_tag):
    print(f"\n🔍 [DB 진단 시작] 검색어: '{target_tag}' 확인 중...")
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # 1. 태그 테이블에 데이터가 있는지 확인 (대소문자 무시)
        print("\n1️⃣ TRACK_TAGS 테이블 조회 결과:")
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

        # 2. 실제 검색 쿼리 시뮬레이션 (app.py와 동일한 로직)
        print(f"\n2️⃣ 검색 API 로직 시뮬레이션 (검색어: {target_tag}):")
        cur.execute("""
            SELECT t.track_title, t.artist_name, tt.tag_id
            FROM TRACKS t 
            JOIN TRACK_TAGS tt ON t.track_id = tt.track_id
            WHERE LOWER(tt.tag_id) = LOWER(:tag)
        """, [target_tag])
        
        rows = cur.fetchall()
        if rows:
            print(f"   🎉 검색 성공! 총 {len(rows)}개의 곡이 조회됩니다.")
            for i, r in enumerate(rows[:5]):
                print(f"   - {i+1}. {r[0]} / {r[1]} (태그: {r[2]})")
        else:
            print("   ⚠️ 검색 결과 0건. (태그는 있지만 정확히 일치하지 않거나, JOIN 할 곡 정보가 없습니다.)")

        # 3. 전체 태그 목록 (참고용)
        print("\n3️⃣ 현재 DB에 저장된 태그 TOP 5:")
        cur.execute("SELECT tag_id, count(*) as c FROM TRACK_TAGS GROUP BY tag_id ORDER BY c DESC FETCH FIRST 5 ROWS ONLY")
        for r in cur.fetchall():
            print(f"   - {r[0]}: {r[1]}개")

    except Exception as e:
        print(f"❌ DB 오류 발생: {e}")
    finally:
        if conn: conn.close()

if __name__ == "__main__":
    # 여기에 확인하고 싶은 태그를 입력하세요
    check_tag_data("tag:jpop")