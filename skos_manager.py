import os
from rdflib import Graph, Namespace, RDF, SKOS

class SkosManager:
    def __init__(self, ttl_file_path):
        self.graph = Graph()
        self.TAG = Namespace("https://knowledgemap.kr/komc/def/tag/")
        
        # 파일 로드
        if os.path.exists(ttl_file_path):
            self.graph.parse(ttl_file_path, format="turtle")
            print(f"✅ SKOS 데이터 로드 완료: {len(self.graph)}개의 관계")
        else:
            print(f"⚠️ SKOS 파일을 찾을 수 없습니다: {ttl_file_path}")

    def get_broader_tags(self, tag_name):
        """
        입력: 'KPop' (또는 'tag:KPop')
        출력: {'KPop', 'Pop', 'Genre'} (자기 자신 포함 상위 개념 모두 반환)
        """
        # 'tag:' 접두사 처리
        clean_name = tag_name.replace("tag:", "")
        subject = self.TAG[clean_name]
        
        # 결과 집합 (중복 제거)
        expanded_tags = set()
        expanded_tags.add(f"tag:{clean_name}") # 자기 자신 추가

        # 재귀적으로 상위 개념(broader) 탐색
        self._find_parents_recursive(subject, expanded_tags)
        
        return list(expanded_tags)

    def _find_parents_recursive(self, concept, result_set):
        # SPARQL 스타일보다 가벼운 방식: 직접 트리플 조회
        # (concept) skos:broader (parent)
        for parent in self.graph.objects(concept, SKOS.broader):
            # URI에서 이름만 추출 (예: .../tag/Pop -> Pop)
            parent_name = parent.split('/')[-1]
            tag_str = f"tag:{parent_name}"
            
            if tag_str not in result_set:
                result_set.add(tag_str)
                # 부모의 부모도 찾으러 감 (재귀)
                self._find_parents_recursive(parent, result_set)

# 테스트용 코드
if __name__ == "__main__":
    manager = SkosManager("skos-definition.ttl")
    print("K-Pop 확장:", manager.get_broader_tags("KPop"))
    print("R&B 확장:", manager.get_broader_tags("ContemporaryRnB"))