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
    
    def _find_concept_uri(self, tag_text):
        """
        태그 텍스트(예: 'jpop')와 일치하는 SKOS 개념 URI를 대소문자 무시하고 찾음
        """
        tag_clean = tag_text.replace("tag:", "").strip().lower()
        
        # 1. 태그 개념(tag_*) 검색
        for s in self.g.subjects(RDF.type, SKOS.Concept):
            # URI 끝부분 추출 (예: .../tag_Jpop -> Jpop)
            uri_suffix = str(s).split("_")[-1].lower()
            if uri_suffix == tag_clean:
                return s
        
        # 2. 장르 개념(Genre_*) 검색 (혹시 Genre_Pop 등을 태그로 썼을 경우)
        for s in self.g.subjects(RDF.type, SKOS.Concept):
            uri_suffix = str(s).split("_")[-1].lower()
            # Genre_Pop -> pop
            if uri_suffix == tag_clean:
                return s
                
        return None

    def get_broader_tags(self, tag):
        """상위 태그 찾기 (예: CityPop -> JPop)"""
        tag_uri = self._find_concept_uri(tag)
        if not tag_uri:
            return set()

        broader_tags = set()
        for parent in self.g.objects(tag_uri, SKOS.broader):
            # 부모 URI에서 이름 추출 (Genre_Pop -> Pop, tag_Retro -> Retro)
            name = str(parent).split("_")[-1]
            broader_tags.add(name)
            
        return broader_tags

    def get_weather_tags(self, weather_keyword):
        """날씨 -> 태그 매핑"""
        weather_uri = self.KOMC[f"Weather_{weather_keyword}"]
        
        # 정의되지 않은 날씨면 Default 사용
        if (weather_uri, RDF.type, SKOS.Concept) not in self.g:
            weather_uri = self.KOMC["Weather_Default"]
            
        related_tags = []
        for related_concept in self.g.objects(weather_uri, SKOS.related):
            for label in self.g.objects(related_concept, SKOS.prefLabel):
                if label.language == 'ko':
                    related_tags.append(str(label))
        return related_tags

    def get_narrower_tags(self, tag):
        """하위 태그 모두 찾기 (재귀) - 검색 확장용"""
        root_concept = self._find_concept_uri(tag)
        all_tags = {tag.replace("tag:", "")} # 자기 자신 포함

        if not root_concept:
            return list(all_tags)

        def find_children(concept):
            for child in self.g.objects(concept, SKOS.narrower):
                # 자식 개념과 관련된 태그 라벨 추출
                # 1. related된 태그 찾기
                for related_tag in self.g.objects(child, SKOS.related):
                    tag_name = str(related_tag).split("_")[-1]
                    if tag_name not in all_tags:
                        all_tags.add(tag_name)
                
                # 2. 자식 개념 자체의 이름도 태그로 간주 (예: Genre_KPop -> KPop)
                child_name = str(child).split("_")[-1]
                if child_name not in all_tags:
                    all_tags.add(child_name)

                # 재귀 호출
                find_children(child)

        find_children(root_concept)
        return list(all_tags)