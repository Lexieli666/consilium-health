"""Skills layer: seven atomic, self-describing tools and the registry that offers them.

| skill | what it does |
|---|---|
| ``search_knowledge`` | general retrieval over the whole corpus |
| ``assess_risk`` | symptom description -> urgency tier and required action |
| ``analyze_symptoms`` | group by body system, single- vs multi-system, notes to read |
| ``recommend_lifestyle`` | diet / activity / sleep / adherence for a named condition |
| ``lookup_disease_code`` | ICD-10 code and chapter for a condition |
| ``find_guideline`` | what a major body recommends on a topic |
| ``deep_research`` | multi-query decomposition with explicit "sources disagree" |

Each is a plain synchronous function with a Pydantic argument model, declared by ``@skill`` and
found by :meth:`SkillRegistry.discover`.  The OpenAI tool schema for each is *derived* from that
argument model; there is no hand-written JSON anywhere in this package.

Importing this module imports the skill modules for their declaration side effects, so
``SkillRegistry.discover()`` is total: it cannot return a registry that is missing a skill because
its module was never imported.
"""

from consilium.skills.base import (
    Passage,
    Skill,
    SkillCategory,
    SkillContext,
    SkillError,
    SkillResult,
    declared_skills,
    doc_ids,
    document_body,
    passages,
    require_retriever,
    skill,
)
from consilium.skills.coding import (
    CodeMention,
    LookupDiseaseCodeArgs,
    LookupDiseaseCodeResult,
    lookup_disease_code,
)
from consilium.skills.guidelines import (
    FindGuidelineArgs,
    FindGuidelineResult,
    GuidelineHit,
    find_guideline,
)
from consilium.skills.knowledge import (
    SearchKnowledgeArgs,
    SearchKnowledgeResult,
    search_knowledge,
)
from consilium.skills.lifestyle import (
    LifestyleDomain,
    RecommendLifestyleArgs,
    RecommendLifestyleResult,
    recommend_lifestyle,
)
from consilium.skills.registry import SkillRegistry
from consilium.skills.research import (
    DeepResearchArgs,
    DeepResearchResult,
    Disagreement,
    Finding,
    deep_research,
)
from consilium.skills.risk import (
    NO_MATCH_ACTION,
    AssessRiskArgs,
    AssessRiskResult,
    MatchedFlag,
    assess_risk,
)
from consilium.skills.symptom_map import SYSTEMS, SymptomMapError, SymptomSystemMap
from consilium.skills.symptoms import (
    AnalyzeSymptomsArgs,
    AnalyzeSymptomsResult,
    CandidateCondition,
    SystemGroup,
    analyze_symptoms,
)

#: The seven skill names, in the order the brief lists them.  Used by tests and by ``policy.yaml``
#: validation to assert that the registry holds exactly these and nothing else.
SKILL_NAMES: tuple[str, ...] = (
    "search_knowledge",
    "assess_risk",
    "analyze_symptoms",
    "recommend_lifestyle",
    "lookup_disease_code",
    "find_guideline",
    "deep_research",
)

__all__ = [
    "NO_MATCH_ACTION",
    "SKILL_NAMES",
    "SYSTEMS",
    "AnalyzeSymptomsArgs",
    "AnalyzeSymptomsResult",
    "AssessRiskArgs",
    "AssessRiskResult",
    "CandidateCondition",
    "CodeMention",
    "DeepResearchArgs",
    "DeepResearchResult",
    "Disagreement",
    "FindGuidelineArgs",
    "FindGuidelineResult",
    "Finding",
    "GuidelineHit",
    "LifestyleDomain",
    "LookupDiseaseCodeArgs",
    "LookupDiseaseCodeResult",
    "MatchedFlag",
    "Passage",
    "RecommendLifestyleArgs",
    "RecommendLifestyleResult",
    "SearchKnowledgeArgs",
    "SearchKnowledgeResult",
    "Skill",
    "SkillCategory",
    "SkillContext",
    "SkillError",
    "SkillRegistry",
    "SkillResult",
    "SymptomMapError",
    "SymptomSystemMap",
    "SystemGroup",
    "analyze_symptoms",
    "assess_risk",
    "declared_skills",
    "deep_research",
    "doc_ids",
    "document_body",
    "find_guideline",
    "lookup_disease_code",
    "passages",
    "recommend_lifestyle",
    "require_retriever",
    "search_knowledge",
    "skill",
]
