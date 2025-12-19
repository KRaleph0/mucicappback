from rdflib import Graph, Namespace, URIRef, Literal
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
    
    # 🚨 [수정됨] 한국어 라벨까지 검색하도록 개선
    def _find_concept_uri(self, tag_text):
        tag_clean = tag_text.replace("tag:", "").strip().lower()
        
        # 1. URI ID로 검색 (예: Pop, Rest)
        for s in self.g.subjects(RDF.type, SKOS.Concept):
            if str(s).split("_")[-1].lower() == tag_clean:
                return s
        
        # 2. 라벨(prefLabel)로 검색 (예: "휴식" -> tag_Rest)
        for s, p, o in self.g.triples((None, SKOS.prefLabel, None)):
            if str(o).lower() == tag_clean:
                return s
                
        return None

    def get_broader_tags(self, tag):
        tag_uri = self._find_concept_uri(tag)
        if not tag_uri: return set()
        broader = set()
        # 상위 개념 찾기
        for parent in self.g.objects(tag_uri, SKOS.broader):
            broader.add(str(parent).split("_")[-1])
        # [추가] 관련된 개념도 상위로 간주하여 추가 (검색 확장성 UP)
        for rel in self.g.objects(tag_uri, SKOS.related):
            broader.add(str(rel).split("_")[-1])
        return broader

    def get_weather_tags(self, weather_keyword):
        uri = self.KOMC[f"Weather_{weather_keyword}"]
        if (uri, RDF.type, SKOS.Concept) not in self.g: uri = self.KOMC["Weather_Default"]
        tags = []
        for rel in self.g.objects(uri, SKOS.related):
            for lbl in self.g.objects(rel, SKOS.prefLabel):
                if lbl.language == 'ko': tags.append(str(lbl))
        return tags

    def get_narrower_tags(self, tag):
        root = self._find_concept_uri(tag)
        # 검색어 자체도 포함
        all_t = {tag.replace("tag:", "")}
        if not root: return list(all_t)
        
        # 3. 루트 개념의 라벨(한국어/영어)도 검색어에 추가
        for lbl in self.g.objects(root, SKOS.prefLabel):
            all_t.add(str(lbl))
        # 루트 개념의 ID도 추가
        all_t.add(str(root).split("_")[-1])

        def find(c):
            for child in self.g.objects(c, SKOS.narrower):
                for rel in self.g.objects(child, SKOS.related):
                    # 관련 태그의 ID와 라벨 모두 추가
                    all_t.add(str(rel).split("_")[-1])
                    for l in self.g.objects(rel, SKOS.prefLabel): all_t.add(str(l))
                
                all_t.add(str(child).split("_")[-1])
                find(child)
        find(root)
        return list(all_t)