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
        tag_clean = tag_text.replace("tag:", "").strip().lower()
        # 태그 개념 검색
        for s in self.g.subjects(RDF.type, SKOS.Concept):
            if str(s).split("_")[-1].lower() == tag_clean:
                return s
        return None

    def get_broader_tags(self, tag):
        tag_uri = self._find_concept_uri(tag)
        if not tag_uri: return set()
        broader = set()
        for parent in self.g.objects(tag_uri, SKOS.broader):
            broader.add(str(parent).split("_")[-1])
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
        all_t = {tag.replace("tag:", "")}
        if not root: return list(all_t)
        def find(c):
            for child in self.g.objects(c, SKOS.narrower):
                for rel in self.g.objects(child, SKOS.related):
                    all_t.add(str(rel).split("_")[-1])
                all_t.add(str(child).split("_")[-1])
                find(child)
        find(root)
        return list(all_t)
