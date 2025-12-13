import oracledb
import config
from skos_manager import SkosManager

def apply_skos_to_existing_tags():
    print("🚀 [SKOS] 기존 태그에 상위 개념(Broader) 적용 시작...")
    
    # 1. SKOS 로드
    try:
        skos = SkosManager("skos-definition.ttl")
    except Exception as e:
        print(f"❌ SKOS 파일 로드 실패: {e}")
        return

    conn = None
    try:
        conn = oracledb.connect(
            user=config.DB_USER,
            password=config.DB_PASSWORD,
            dsn=config.DB_DSN
        )
        cur = conn.cursor()

        # 2. 현재 DB에 있는 모든 태그 가져오기
        print("   📂 DB에서 태그 목록 조회 중...")
        cur.execute("SELECT track_id, tag_id FROM TRACK_TAGS")
        existing_tags = cur.fetchall()
        
        added_count = 0
        
        # 3. 각 태그별로 상위 개념 찾아서 추가
        for track_id, tag_id in existing_tags:
            # tag:Jpop -> tag:Jpop (유지), tag:CityPop -> {tag:Jpop, tag:Retro...}
            broader_tags = skos.get_broader_tags(tag_id)
            
            for parent_tag in broader_tags:
                parent_tag_id = f"tag:{parent_tag}" if not parent_tag.startswith("tag:") else parent_tag
                
                # 중복 방지 (MERGE)
                try:
                    cur.execute("""
                        MERGE INTO TRACK_TAGS t 
                        USING (SELECT :1 a, :2 b FROM dual) s 
                        ON (t.track_id=s.a AND t.tag_id=s.b) 
                        WHEN NOT MATCHED THEN INSERT (track_id, tag_id) VALUES (s.a, s.b)
                    """, [track_id, parent_tag_id])
                    
                    if cur.rowcount > 0:
                        print(f"   ➕ [확장] {tag_id} -> {parent_tag_id} 추가됨 (Track: {track_id[:5]}...)")
                        added_count += 1
                except Exception as e:
                    pass

        conn.commit()
        print(f"\n🎉 작업 완료! 총 {added_count}개의 상위 태그가 자동으로 추가되었습니다.")

    except Exception as e:
        print(f"❌ DB 오류: {e}")
    finally:
        if conn: conn.close()

if __name__ == "__main__":
    apply_skos_to_existing_tags()