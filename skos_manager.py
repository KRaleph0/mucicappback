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
        """기존 기능: 상위 태그 찾기"""
        tag = tag.replace("tag:", "")
        tag_uri = self.KOMC[f"tag_{tag}"]
        
        broader_tags = set()
        for parent in self.g.objects(tag_uri, SKOS.broader):
            # URI에서 라벨 추출 (예: komc:tag_Jpop -> tag:Jpop)
            label = parent.split('/')[-1].replace('tag_', 'tag:')
            broader_tags.add(label)
            
        return broader_tags

    def get_weather_tags(self, weather_keyword):
        """
        [NEW] 날씨 키워드를 받아서 skos:related로 연결된 태그들의 '한글 라벨'을 반환
        예: 'Rain' -> ['비오는날', '감성', '우울', '잔잔한']
        """
        # TTL에 정의된 URI 패턴: komc:Weather_Rain, komc:Weather_Clear ...
        weather_uri = self.KOMC[f"Weather_{weather_keyword}"]
        
        related_tags = []
        
        # 1. 해당 날씨 개념이 있는지 확인
        if (weather_uri, RDF.type, SKOS.Concept) not in self.g:
            print(f"⚠️ SKOS: Undefined weather '{weather_keyword}', using Default.")
            weather_uri = self.KOMC["Weather_Default"]

        # 2. skos:related로 연결된 태그 찾기
        for related_concept in self.g.objects(weather_uri, SKOS.related):
            # 3. 그 태그의 한글 라벨(prefLabel) 가져오기
            for label in self.g.objects(related_concept, SKOS.prefLabel):
                if label.language == 'ko':
                    related_tags.append(str(label))
        
        print(f"🔍 SKOS Weather Mapping: {weather_keyword} -> {related_tags}")
        return related_tags