from rdflib import Graph, Namespace, URIRef
from rdflib.namespace import SKOS, RDF

class SkosManager:
    def __init__(self, file_path):
        self.g = Graph()
        try:
            self.g.parse(file_path, format="turtle")
            print(f"✅ SKOS data loaded from {file_path}")
        except Exception as e:
            print(f"❌ Failed to load SKOS data: {e}")

        self.KOMC = Namespace("https://knowledgemap.kr/komc/def/")
    
    def get_broader_tags(self, tag):
        """상위 태그 찾기 (예: CityPop -> JPop)"""
        tag = tag.replace("tag:", "")
        tag_uri = self.KOMC[f"tag_{tag}"]
        if (tag_uri, RDF.type, SKOS.Concept) not in self.g:
             # 태그가 아니라 장르 개념일 수도 있으므로 확인 (예: Genre_CityPop)
             tag_uri = self.KOMC[f"Genre_{tag}"]

        broader_tags = set()
        for parent in self.g.objects(tag_uri, SKOS.broader):
            # 부모 개념이 Genre_JPop 형태라면 -> tag:Jpop으로 변환해야 함
            # 단순화를 위해 related된 태그를 찾거나, 이름을 파싱
            parent_name = parent.split('/')[-1].replace('Genre_', '').replace('tag_', '')
            broader_tags.add(parent_name)
            
        return broader_tags

    def get_weather_tags(self, weather_keyword):
        """날씨 -> 태그 매핑"""
        weather_uri = self.KOMC[f"Weather_{weather_keyword}"]
        if (weather_uri, RDF.type, SKOS.Concept) not in self.g:
            weather_uri = self.KOMC["Weather_Default"]
            
        related_tags = []
        for related_concept in self.g.objects(weather_uri, SKOS.related):
            for label in self.g.objects(related_concept, SKOS.prefLabel):
                if label.language == 'ko':
                    related_tags.append(str(label))
        return related_tags

    # [NEW] 하위 태그 모두 찾기 (재귀 탐색)
    def get_narrower_tags(self, tag):
        """
        입력: 'Pop'
        출력: {'Pop', 'Jpop', 'Kpop', 'CityPop', ...} (자신 포함 모든 하위 태그)
        """
        tag = tag.replace("tag:", "")
        # 입력이 태그 이름일 수도 있고 장르 이름일 수도 있음
        # SKOS에서 해당 라벨을 가진 개념(Concept)을 찾음
        root_concept = None
        
        # 1. 개념 찾기 (prefLabel이 일치하는 것)
        for s, p, o in self.g.triples((None, SKOS.prefLabel, None)):
            if str(o).lower() == tag.lower() or str(o).replace("-", "").lower() == tag.replace("-", "").lower():
                root_concept = s
                break
        
        # 못 찾았으면 이름으로 추정 (Genre_Pop 등)
        if not root_concept:
            root_concept = self.KOMC[f"Genre_{tag}"]

        all_tags = {tag} # 자기 자신 포함

        if not root_concept:
            return all_tags

        # 2. 하위 개념 재귀 탐색
        def find_children(concept):
            for child in self.g.objects(concept, SKOS.narrower):
                # 자식 개념의 '관련 태그(related)' 정보를 가져옴
                for related_tag in self.g.objects(child, SKOS.related):
                    # komc:tag_Jpop -> "Jpop" 추출
                    tag_code = related_tag.split('/')[-1].replace('tag_', '')
                    if tag_code not in all_tags:
                        all_tags.add(tag_code)
                        # print(f"   [확장] {tag} -> {tag_code}")
                
                # 재귀 호출
                find_children(child)

        find_children(root_concept)
        return list(all_tags)