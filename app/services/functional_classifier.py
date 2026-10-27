"""What does this job actually DO?

The old classifier answered "what did HR call it?". `ROLE_PATTERNS` in
`classifiers.py` is a list of title regexes; when a title matched nothing it
fell back to searching the description — for the same *title* patterns. A
posting reading "SQL, Python, KPI analysis, experiment design, partner with
product on metrics" contains none of the strings `data scientist`,
`data analyst` or `machine learning engineer`, so it classified as
`RoleFamily.UNRELATED`.

That is not a rare edge. **9,931 of 10,424 collected jobs were `UNRELATED`** —
95% of the corpus — and the roles the candidate is best suited to are disproportionately
the ones with titles like "Strategy & Operations Associate", "Marketplace
Analyst" and "Decision Science Associate".

So this module classifies **function**, from the description, with the title as
one signal among several:

1. **Title triage** — CLEAR_RELEVANT, AMBIGUOUS (read the JD), CLEAR_IRRELEVANT.
   Only an occupation that is unambiguously something else is rejected on its
   title. "Marketplace Strategy Associate" is ambiguous, not irrelevant.
2. **Capability extraction** — what the JD says the person will do, in
   capability families rather than exact keywords.
3. **Negative function signals** — a job is not analytics because it mentions a
   dashboard. Sales, support, QA, recruiting, accounting and clinical work are
   excluded on what the job *is*, not on the domain it sits in.
4. **Function scoring** — capabilities and JD signals decide the function; the
   title adjusts, and can be overruled.

**The domain is not the function.** Finance, healthcare, risk and operations all
contain excellent analytics roles and all contain roles with no data content at
all. `Financial Analyst` doing FP&A and budgeting is not a data role; `Risk
Strategy Analyst` writing SQL and building models is. Only the work decides.

Deterministic and cheap: regex and set arithmetic over stored text, no I/O and
no model call. It runs over the whole corpus in seconds, which is what lets it
sit in front of the expensive stages rather than behind them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.config import load_yaml_config

#: Bumped when the rules below change, so stored classifications can be
#: recomputed and compared. Stored on every job this touches.
#: 1.1.0 — SOFTWARE_ENGINEERING became a real (secondary) target function, with
#: specialisation rules deciding which software roles the evidence supports.
FUNCTIONAL_CLASSIFIER_VERSION = "1.1.0"

STRATEGY_CONFIG = "search_strategy.yaml"


# ---------------------------------------------------------------------------
# Title triage
# ---------------------------------------------------------------------------

TITLE_CLEAR_RELEVANT = "TITLE_CLEAR_RELEVANT"
TITLE_AMBIGUOUS_REQUIRES_JD_ANALYSIS = "TITLE_AMBIGUOUS_REQUIRES_JD_ANALYSIS"
TITLE_CLEAR_IRRELEVANT = "TITLE_CLEAR_IRRELEVANT"

#: Titles that name the target work outright. These skip straight to scoring.
_TITLE_CLEARLY_RELEVANT = re.compile(
    r"\b(data scien(ce|tist)|applied scientist|research scientist|"
    r"machine learning engineer|ml engineer|\bmle\b|ai engineer|applied ai|"
    r"data analyst|business intelligence|\bbi\b engineer|analytics engineer|"
    r"data engineer|analytics? (?:analyst|associate|manager|lead)|"
    r"product analyst|product analytics|quantitative analyst|"
    r"decision scien(ce|tist)|insights analyst|business analyst|"
    r"statistician|biostatistician|econometric|"
    # Software titles joined this list on 2026-09-07. They name the function
    # outright, which is exactly what CLEAR_RELEVANT means — and it keeps
    # "Software Engineer" out of HIDDEN FIT, which is reserved for titles that
    # hide the work. "Product Engineer, Data Systems" stays ambiguous, and can
    # still be a hidden fit. D-49.
    r"software engineer|software developer|software development engineer|"
    r"backend engineer|back[- ]end engineer|platform engineer|"
    r"\bsde\b|\bsdet\b)\b",
    re.I,
)

#: Occupations that are unambiguously something else. A title matching one of
#: these is rejected WITHOUT reading the JD — the only place in this module
#: where that happens, which is why the list is narrow and specific.
#:
#: Deliberately absent: "analyst", "associate", "strategy", "operations",
#: "risk", "pricing", "marketing", "finance", "healthcare". Every one of those
#: appears in titles of real target roles.
_TITLE_CLEARLY_IRRELEVANT = re.compile(
    r"\b(nurse|nursing|physician|surgeon|dentist|pharmacist|therapist|"
    r"veterinar\w+|radiolog\w+|clinician|caregiver|phlebotom\w+|"
    r"chef|cook|barista|server|bartender|housekeep\w+|janitor|custodian|"
    r"driver|courier|warehouse|forklift|welder|electrician|plumber|carpenter|"
    r"machinist|hvac|automotive technician|"
    r"account executive|sales (?:representative|rep|manager|director|executive|associate)|"
    r"business development (?:representative|manager)|\bsdr\b|\bbdr\b|"
    r"inside sales|outside sales|"
    # "Customer Experience Representative" reached the active SWE list in the
    # D-49 audit: the support alternation named success/support/service but
    # not "experience", and Robinhood calls its support role that.
    r"customer (?:success|support|service|experience) (?:representative|associate|specialist|advocate|agent|engineer|analyst|manager)|"
    r"technical support (?:engineer|specialist|representative)|help ?desk|"
    r"sales (?:systems?|operations?|enablement) (?:engineer|analyst|manager|associate)|"
    r"recruiter|talent acquisition|hr (?:business partner|generalist)|payroll|"
    r"paralegal|attorney|counsel|litigation|"
    r"copywriter|content writer|social media (?:manager|coordinator)|"
    r"graphic designer|ux designer|brand manager|"
    r"real estate|leasing|retail associate|store manager|cashier|"
    r"security guard|dispatcher|flight attendant|pilot|"
    r"executive assistant|administrative assistant|office manager|receptionist|"
    r"staff accountant|bookkeep\w+|accounts payable|accounts receivable|"
    r"tax (?:associate|manager|accountant|attorney)|auditor)\b",
    re.I,
)

#: Seniority in the title. Not irrelevance — an experience band.
_TITLE_SENIOR = re.compile(
    r"\b(senior|sr\.?|staff|principal|distinguished|fellow|lead|"
    r"director|vice president|\bvp\b|\bsvp\b|\bevp\b|head of|chief|"
    r"\bcto\b|\bceo\b|\bcio\b|manager)\b",
    re.I,
)


# ---------------------------------------------------------------------------
# Capability families
# ---------------------------------------------------------------------------
#
# What the person will DO, grouped so that no single keyword is required. Each
# family carries a weight reflecting how much it distinguishes a data role from
# a role that merely touches data: writing SQL is a strong signal, "reporting"
# on its own is weak because every job reports something.

CapabilitySpec = tuple[str, int, re.Pattern[str]]

CAPABILITY_FAMILIES: list[CapabilitySpec] = [
    ("SQL", 4, re.compile(r"\b(sql|postgres\w*|mysql|snowflake|redshift|bigquery|"
                          r"querying (?:data|databases)|write queries|writing queries)\b", re.I)),
    # The R language is named explicitly rather than as a bare `\br\b`, which
    # matched the "R" in "R&D team" and handed PROGRAMMING to a supplier
    # engineering posting. A one-letter token needs company to be readable.
    ("PROGRAMMING", 4, re.compile(r"\b(python|pandas|numpy|scikit[- ]?learn|jupyter|scala|"
                                  r"r programming|programming in r|python/r|r/python|"
                                  r"(?:in|using|with) r\b)", re.I)),
    ("STATISTICAL_ANALYSIS", 3, re.compile(r"\b(statistic\w*|regression|hypothesis test\w*|"
                                           r"significance|confidence interval|bayesian|"
                                           r"causal inference|econometric\w*)\b", re.I)),
    ("PREDICTIVE_MODELING", 4, re.compile(r"\b(predictive model\w*|machine learning|forecast\w*|"
                                          r"classification model|regression model|"
                                          r"model (?:development|training|building)|"
                                          r"xgboost|random forest|gradient boost\w*)\b", re.I)),
    ("MACHINE_LEARNING", 4, re.compile(r"\b(machine learning|deep learning|neural network|"
                                       r"\bnlp\b|computer vision|\bllm\b|"
                                       r"large language model|generative ai|transformer)\b", re.I)),
    ("EXPERIMENTATION", 4, re.compile(r"\b(a/b test\w*|ab test\w*|experiment\w*|"
                                      r"causal impact|incrementality|holdout|"
                                      r"randomi[sz]ed|test and learn|measurement framework)\b", re.I)),
    ("METRICS_KPI", 3, re.compile(r"\b(kpis?|key performance indicator|metrics?|"
                                  r"north star|success measures?|"
                                  r"define (?:and )?(?:track|measure))\b", re.I)),
    ("PRODUCT_ANALYTICS", 3, re.compile(r"\b(funnel|cohort|retention|churn|engagement|"
                                        r"conversion|user behavio\w+|product analytics|"
                                        r"activation|\bdau\b|\bmau\b|lifecycle)\b", re.I)),
    ("BUSINESS_INSIGHT", 2, re.compile(r"\b(insights?|recommendations?|inform decisions?|"
                                       r"decision[- ]making|business impact|drive strategy|"
                                       r"actionable)\b", re.I)),
    ("STAKEHOLDER_ANALYSIS", 2, re.compile(r"\b(stakeholders?|cross[- ]functional|partner with|"
                                           r"business partners?|present (?:findings|results)|"
                                           r"communicate (?:findings|results|insights))\b", re.I)),
    ("DASHBOARDING_BI", 2, re.compile(r"\b(dashboard\w*|tableau|looker|power ?bi|mode analytics|"
                                      r"sigma computing|visuali[sz]ation|reporting layer|"
                                      r"self[- ]serve)\b", re.I)),
    ("DATA_MODELING", 3, re.compile(r"\b(data model\w*|dimensional model\w*|star schema|"
                                    r"schema design|canonical (?:dataset|table)|\bdbt\b|"
                                    r"semantic layer)\b", re.I)),
    ("ETL_PIPELINES", 3, re.compile(r"\b(etl|elt|data pipeline\w*|ingestion|"
                                    r"airflow|dagster|prefect|orchestrat\w+|"
                                    r"batch (?:job|process)|streaming)\b", re.I)),
    ("BIG_DATA", 3, re.compile(r"\b(spark|pyspark|hadoop|databricks|kafka|"
                               r"distributed (?:computing|processing)|large[- ]scale data)\b", re.I)),
    ("DATA_QUALITY", 2, re.compile(r"\b(data quality|data integrity|data valid\w+|"
                                   r"data governance|anomaly detection|"
                                   r"data accuracy|reconcil\w+)\b", re.I)),
    ("DATA_WRANGLING", 2, re.compile(r"\b(data clean\w*|data wrangl\w*|feature engineering|"
                                     r"data preparation|data manipulation|"
                                     r"extract\w* (?:and )?transform\w*)\b", re.I)),
    ("WAREHOUSE", 2, re.compile(r"\b(data warehouse|data lake|lakehouse|"
                                r"warehousing|olap)\b", re.I)),
    ("OPTIMIZATION", 3, re.compile(r"\b(optimi[sz]ation|linear programming|"
                                   r"simulation|operations research|"
                                   r"supply[- ]demand|marketplace dynamics)\b", re.I)),
    ("RISK_PRICING", 3, re.compile(r"\b(risk model\w*|credit risk|fraud (?:detection|model)|"
                                   r"underwriting|pricing (?:model|strategy|optimi[sz]ation)|"
                                   r"loss forecast\w*|scorecard)\b", re.I)),
    # Software and infrastructure are capabilities too, and they belong here for
    # a specific reason: without them, a backend JD mentioning "Python" and
    # "metrics" scored as analytics. The audit surfaced "Backend Engineer, Core
    # Technology" classified as MACHINE_LEARNING_ENGINEERING and "Software
    # Engineer II, Fullstack" as PRODUCT_ANALYTICS.
    #
    # Naming software work lets a software posting be recognised AS software.
    # That is ALL it does. Until 2026-09-07 it also excluded the posting,
    # because SOFTWARE_ENGINEERING was not an active function — a correct
    # classification wired to the wrong consequence, since the candidate's OpenText
    # internship is itself a software engineering job. D-49.
    #
    # NOTE the plurals. `backend (?:service|system)` inside a trailing-`\b`
    # alternation cannot match "backend services": the boundary would have to
    # fall between "e" and "s", and there is none. SOFTWARE_BUILD therefore
    # never fired at all, and Robinhood's "Software Engineer, Backend"
    # classified as DATA_ENGINEERING. Same defect class as the "Sr." title bug.
    ("SOFTWARE_BUILD", 5, re.compile(r"\b(microservices?|\bapis?\b|rest apis?|graphql|"
                                     r"backend (?:services?|systems?|engineer)|frontend|front[- ]end|"
                                     r"full[- ]stack|react|typescript|javascript|"
                                     r"distributed systems?|service[- ]oriented|"
                                     r"production (?:code|services?)|code review|"
                                     r"software (?:design|architecture)|latency|throughput at scale|"
                                     r"golang|\bgo\b programming|java\b|c\+\+|kotlin|swift)\b", re.I)),
    ("INFRASTRUCTURE", 4, re.compile(r"\b(kubernetes|docker|terraform|\bci/cd\b|"
                                     r"infrastructure as code|observability|"
                                     r"site reliability|on[- ]call rotation|"
                                     r"provisioning|deployment pipeline)\b", re.I)),
]

#: Wording that marks a line as a real requirement or responsibility rather than
#: company prose. A single capability mention inside one of these counts; a
#: single mention anywhere else does not.
_REQUIREMENT_CONTEXT = re.compile(
    # Only genuine requirement markers. An earlier version also accepted the
    # bare verbs build / design / analyse, which appear in every job description
    # ever written and let the boilerplate straight back in through a second
    # door: "expected to experiment boldly. Design CAD layouts..." counted.
    r"(?:experience (?:with|in|using)|proficien\w+ (?:with|in)|familiar\w* with|"
    # "hands-on" removed: "This is a hands-on role with the opportunity to
    # drive impact through experimentation" is company prose, and it was
    # enough to give a Battery Systems Engineer the EXPERIMENTATION capability.
    r"skills? in|knowledge of|expertise in|working knowledge|"
    r"comfortable (?:with|using)|strong (?:command|background|grasp)|"
    r"background in|fluency in|track record (?:of|in)|demonstrated ability)",
    re.I,
)

#: Specific named technologies. A capability whose single mention IS one of
#: these counts on its own, because nobody writes "PostgreSQL" or "Entity
#: Framework" in a culture paragraph — a stack list is a requirement even when
#: it names each tool once.
#:
#: This is the other half of the two-mentions rule. That rule exists because
#: "expected to experiment boldly" appeared in every SharkNinja posting; it does
#: not need to fire on "C# and .NET, Entity Framework, REST APIs and SQL Server",
#: which is the candidate's OpenText stack listed exactly once each. D-49.
_NAMED_TECHNOLOGY = re.compile(
    r"\b(python|pandas|numpy|scikit[- ]?learn|jupyter|scala|"
    r"postgres\w*|mysql|oracle|sql server|snowflake|redshift|bigquery|\bsql\b|"
    r"c#|\.net|entity framework|asp\.net|"
    r"spark|pyspark|hadoop|databricks|kafka|"
    r"airflow|dagster|prefect|\bdbt\b|"
    r"kubernetes|docker|terraform|openshift|"
    r"xgboost|random forest|tableau|looker|power ?bi|"
    r"rest apis?|graphql|microservices?|"
    r"react|typescript|javascript|golang|java|c\+\+|kotlin|swift|"
    r"postman|swagger|mstest|azure devops)\b",
    re.I,
)

#: How many core capabilities a description must show before the job counts as
#: data work. Raised from 1 to 2 after the first retrospective audit: at 1, a
#: single "SQL" in a backend engineering posting was enough.
MIN_CORE_CAPABILITIES = 2

PRIMARY_TARGET = "PRIMARY_TARGET"
SECONDARY_TARGET = "SECONDARY_TARGET"
DEPRIORITIZED = "DEPRIORITIZED"

#: Capabilities that make a SOFTWARE posting a fit for this candidate. The
#: analytics core set is the wrong bar for a backend role — a service with a
#: PostgreSQL database and a REST API is squarely on the candidate's OpenText
#: experience and contains no "experimentation" or "predictive modelling" at
#: all. Two of these, same as the analytics bar.
_SWE_CORE_CAPABILITIES = frozenset(
    {
        "SOFTWARE_BUILD",
        "PROGRAMMING",
        "SQL",
        "ETL_PIPELINES",
        "INFRASTRUCTURE",
        "BIG_DATA",
        "DATA_MODELING",
        "DATA_QUALITY",
    }
)

#: Capabilities that, on their own, indicate genuinely data-intensive work.
#: A posting needs at least one of these to be a functional match — otherwise
#: "metrics" and "insights" alone would admit every business role in the corpus.
_CORE_CAPABILITIES = frozenset(
    {
        "SQL",
        "PROGRAMMING",
        "STATISTICAL_ANALYSIS",
        "PREDICTIVE_MODELING",
        "MACHINE_LEARNING",
        "EXPERIMENTATION",
        "DATA_MODELING",
        "ETL_PIPELINES",
        "BIG_DATA",
        "OPTIMIZATION",
        "RISK_PRICING",
    }
)


# ---------------------------------------------------------------------------
# Negative function signals
# ---------------------------------------------------------------------------
#
# What the job IS, when that is something else. These fire on descriptions of
# the actual occupation, not on the domain or on incidental vocabulary.

NegativeSpec = tuple[str, int, re.Pattern[str]]

NEGATIVE_FUNCTION_SIGNALS: list[NegativeSpec] = [
    ("SALES", 5, re.compile(r"\b(quota|close deals|sales cycle|prospect\w+|cold call\w*|"
                            r"book of business|pipeline generation|upsell|"
                            r"account (?:executive|management)|revenue targets?)\b", re.I)),
    ("CUSTOMER_SUPPORT", 4, re.compile(r"\b(support tickets?|help ?desk|customer inquiries|"
                                       r"troubleshoot customer|service level agreement|"
                                       r"resolve (?:customer )?issues|zendesk|"
                                       r"customer onboarding|account health)\b", re.I)),
    ("MANUAL_QA", 4, re.compile(r"\b(test cases?|test plans?|manual testing|"
                                r"regression testing|defect tracking|"
                                r"quality assurance (?:engineer|analyst|tester))\b", re.I)),
    ("IT_SUPPORT", 5, re.compile(r"\b(help ?desk|desktop support|system administration|"
                                 r"active directory|network administration|"
                                 r"end[- ]user support|it support)\b", re.I)),
    ("RECRUITING_HR", 5, re.compile(r"\b(sourcing candidates|candidate pipeline|"
                                    r"talent acquisition|full[- ]cycle recruiting|"
                                    r"employee relations|benefits administration|"
                                    r"onboarding new hires)\b", re.I)),
    ("PROJECT_COORDINATION", 3, re.compile(r"\b(project coordinat\w+|schedul\w+ meetings|"
                                           r"meeting minutes|status reports?|"
                                           r"calendar management|administrative support)\b", re.I)),
    ("LEGAL", 5, re.compile(r"\b(legal counsel|contracts? (?:review|negotiation)|"
                            r"litigation|regulatory filings?|patent|"
                            r"juris doctor|\bjd\b degree|bar admission)\b", re.I)),
    ("ACCOUNTING", 5, re.compile(r"\b(\bcpa\b|generally accepted accounting|\bgaap\b|"
                                 r"journal entries|month[- ]end close|"
                                 r"accounts (?:payable|receivable)|bookkeep\w+|"
                                 r"general ledger|financial statements? preparation|"
                                 r"audit (?:engagement|procedures)|tax returns?)\b", re.I)),
    ("CORPORATE_FINANCE", 3, re.compile(r"\b(fp&a|financial planning and analysis|"
                                        r"budgeting and forecasting process|variance analysis|"
                                        r"investment banking|\bm&a\b|"
                                        r"capital markets|treasury operations)\b", re.I)),
    ("COMPLIANCE_ONLY", 3, re.compile(r"\b(compliance program|regulatory compliance|"
                                      r"policy enforcement|internal controls|"
                                      r"sox compliance|kyc|aml program)\b", re.I)),
    ("CLINICAL", 5, re.compile(r"\b(patient care|clinical practice|licensed (?:nurse|physician)|"
                               r"\brn\b|\bmd\b required|board certified|"
                               r"bedside|direct patient)\b", re.I)),
    ("MARKETING_CONTENT", 3, re.compile(r"\b(content calendar|copywriting|brand voice|"
                                        r"social media posts?|campaign creative|"
                                        r"editorial|seo content|influencer)\b", re.I)),
    ("HARDWARE", 4, re.compile(r"\b(pcb|circuit design|firmware|embedded systems|"
                               r"mechanical design|cad|soldering|"
                               r"hardware validation)\b", re.I)),
    # All three surfaced in the first audit as recovered "analytics" roles:
    # Solutions Architect, Sr. Developer Advocate, Vendor Security Manager.
    # Each talks about data fluently and none of them is a data job.
    ("SOLUTIONS_SALES_ENGINEERING", 4, re.compile(
        r"\b(solutions? (?:architect|engineer|consultant)|sales engineer\w*|"
        r"pre[- ]?sales|customer[- ]facing technical|technical account manager|"
        r"proof of concept for (?:customers|clients)|customer deployments?)\b", re.I)),
    ("DEVELOPER_RELATIONS", 4, re.compile(
        r"\b(developer advocate|developer relations|devrel|community manager|"
        r"technical evangelis\w+|conference talks|blog posts and tutorials)\b", re.I)),
    ("SECURITY_OPERATIONS", 4, re.compile(
        r"\b(security operations|incident response|threat detection|"
        r"vulnerability management|penetration testing|\bsoc\b analyst|"
        r"security questionnaires?|vendor security)\b", re.I)),
    # "Platform Administrator (Salesforce Service Cloud)" and "GTM Systems
    # Engineer, Salesforce" both reached the active software list in the D-49
    # audit. Configuring a SaaS CRM is a real job and not this one.
    ("SAAS_ADMINISTRATION", 4, re.compile(
        r"\b(salesforce|servicenow|workday|netsuite|hubspot|zendesk|"
        r"crm administration|system administrator|platform administrator|"
        r"\bccaas\b|service cloud|apex triggers?)\b", re.I)),
    ("ESCALATIONS_SUPPORT", 4, re.compile(
        r"\b(technical escalations?|customer escalations?|escalations? (?:team|queue)|"
        r"tier [23] support|support queue|escalation management)\b", re.I)),
    ("PROGRAM_PRODUCT_MANAGEMENT", 2, re.compile(
        r"\b(product roadmap|product requirements document|\bprd\b|"
        r"go[- ]to[- ]market|product strategy|user stories|backlog grooming)\b", re.I)),
]

#: A negative family this strong outweighs any amount of analytics vocabulary:
#: these describe the whole occupation, not a part of it.
_DECISIVE_NEGATIVES = frozenset(
    {
        "SALES",
        "IT_SUPPORT",
        "RECRUITING_HR",
        "LEGAL",
        "ACCOUNTING",
        "CLINICAL",
        # Added after the audit recovered a "Corporate FP&A Analyst": budgeting,
        # forecasting and month-end close is finance work, and the word
        # "forecast" is not a modelling capability in that context.
        "CORPORATE_FINANCE",
        "SOLUTIONS_SALES_ENGINEERING",
        "SECURITY_OPERATIONS",
        "SAAS_ADMINISTRATION",
        "ESCALATIONS_SUPPORT",
    }
)


# ---------------------------------------------------------------------------
# Software specialisation
# ---------------------------------------------------------------------------
#
# Software engineering is a target; not every software engineering job is.
# the candidate's evidence is backend, APIs, services, C#/.NET, Python, SQL and
# relational databases, plus data/ML platform work from his projects. A React
# component library, an iOS app and a firmware bring-up are all software, and
# none of them is that.
#
# This decides SPECIALISATION only. Seniority is a separate axis and still
# binds: an aligned backend role asking for 7 years is still OVERLEVELLED.

#: A title that names an engineering job. Used to decide whether a
#: specialisation word in the title is about the ROLE or merely the domain.
_ENGINEERING_TITLE = re.compile(r"\b(engineer|engineering|developer|architect)\b", re.I)

#: Specialisation words as they appear in a TITLE, where a bare token is enough.
#: The body patterns need context — a backend JD may mention frontend engineers
#: as collaborators — but "Software Engineer, Frontend, Payments Revenue" names the
#: specialisation with a bare comma-separated token and reached the active list
#: because the body pattern wanted "frontend engineer".
_TITLE_SPECIALIZATION: list[tuple[str, re.Pattern[str]]] = [
    ("FRONTEND", re.compile(r"\b(front[- ]?end|\bui\b|web ?ui)\b", re.I)),
    ("MOBILE", re.compile(r"\b(ios|android|mobile|react native|flutter)\b", re.I)),
    ("EMBEDDED", re.compile(r"\b(embedded|firmware)\b", re.I)),
    ("GRAPHICS_GAME", re.compile(r"\b(graphics|game|rendering)\b", re.I)),
    ("LOW_LEVEL", re.compile(r"\b(kernel|compiler|systems programming)\b", re.I)),
    ("NETWORKING", re.compile(r"\bnetwork(?:ing)?\b", re.I)),
    ("SECURITY_ENGINEERING", re.compile(r"\b(security|appsec|cryptograph\w+)\b", re.I)),
]

SPECIALIZATION_ALIGNED = "ALIGNED"
SPECIALIZATION_NEUTRAL = "NEUTRAL"
SPECIALIZATION_MISMATCHED = "MISMATCHED"

#: Software work the evidence supports.
_SWE_ALIGNED = re.compile(
    r"\b(backend|back[- ]end|server[- ]side|api development|rest apis?|"
    r"web services?|microservices?|service architecture|"
    r"c#|\.net|entity framework|asp\.net|"
    r"python|django|flask|fastapi|"
    r"sql server|postgres\w*|mysql|oracle|relational databases?|"
    r"database[- ]backed|\borm\b|stored procedures?|"
    r"data platform|data infrastructure|data pipelines?|"
    r"ml platform|ml infrastructure|machine learning (?:platform|infrastructure|systems)|"
    r"kafka|spark|airflow|"
    r"integration tests?|unit tests?|test automation|"
    r"docker|kubernetes|openshift)\b",
    re.I,
)

#: Software work the evidence does not support. Each of these describes a
#: specialisation, not a technology mentioned in passing.
_SWE_MISMATCHED: list[tuple[str, re.Pattern[str]]] = [
    ("FRONTEND", re.compile(r"\b(front[- ]?end (?:engineer|developer|development)|"
                            r"ui engineer|design systems?|css|tailwind|"
                            r"component library|responsive design|"
                            r"react (?:developer|engineer)|web accessibility)\b", re.I)),
    ("MOBILE", re.compile(r"\b(ios|android|swift|swiftui|uikit|kotlin|"
                          r"react native|flutter|mobile (?:app|engineer|development)|"
                          r"app store|xcode)\b", re.I)),
    ("EMBEDDED", re.compile(r"\b(embedded|firmware|rtos|microcontroller|bare[- ]metal|"
                            r"device drivers?|hardware abstraction|bootloader)\b", re.I)),
    ("GRAPHICS_GAME", re.compile(r"\b(game engine|unreal|unity3d|opengl|vulkan|directx|"
                                 r"shader|rendering pipeline|3d graphics)\b", re.I)),
    ("LOW_LEVEL", re.compile(r"\b(kernel|compiler (?:design|engineer)|assembly language|"
                             r"memory management|operating systems? internals|"
                             r"linker|instruction set)\b", re.I)),
    ("NETWORKING", re.compile(r"\b(network (?:engineer|infrastructure|protocols?)|"
                              r"\bbgp\b|\bospf\b|routing protocols?|switching|"
                              r"load balancer configuration)\b", re.I)),
    ("SECURITY_ENGINEERING", re.compile(r"\b(cryptograph\w+|penetration testing|"
                                        r"exploit|malware|reverse engineering|"
                                        r"security engineer|appsec)\b", re.I)),
]


# ---------------------------------------------------------------------------
# Function signals
# ---------------------------------------------------------------------------
#
# Which of the target functions the work belongs to, given that it IS data work.
# Scored from the description; the title contributes but never decides alone.

FunctionSpec = tuple[str, dict[str, int]]

FUNCTION_SIGNALS: dict[str, dict[str, int]] = {
    "DATA_SCIENCE": {
        "PREDICTIVE_MODELING": 4, "STATISTICAL_ANALYSIS": 4, "MACHINE_LEARNING": 3,
        "EXPERIMENTATION": 3, "PROGRAMMING": 2, "SQL": 1,
    },
    "MACHINE_LEARNING_ENGINEERING": {
        "MACHINE_LEARNING": 5, "PREDICTIVE_MODELING": 3, "ETL_PIPELINES": 2,
        "BIG_DATA": 2, "PROGRAMMING": 2,
    },
    "APPLIED_AI_SOFTWARE": {
        "MACHINE_LEARNING": 4, "PROGRAMMING": 3, "ETL_PIPELINES": 1,
    },
    "DATA_ANALYTICS": {
        "SQL": 4, "BUSINESS_INSIGHT": 3, "METRICS_KPI": 3, "STAKEHOLDER_ANALYSIS": 3,
        "DASHBOARDING_BI": 2, "DATA_WRANGLING": 2, "STATISTICAL_ANALYSIS": 1,
    },
    "PRODUCT_ANALYTICS": {
        "PRODUCT_ANALYTICS": 5, "EXPERIMENTATION": 4, "METRICS_KPI": 3,
        "SQL": 3, "BUSINESS_INSIGHT": 2,
    },
    "DECISION_ANALYTICS": {
        "OPTIMIZATION": 4, "RISK_PRICING": 5, "STATISTICAL_ANALYSIS": 3,
        "PREDICTIVE_MODELING": 3, "BUSINESS_INSIGHT": 2, "SQL": 2,
    },
    "ANALYTICS_ENGINEERING": {
        "DATA_MODELING": 5, "DATA_QUALITY": 3, "ETL_PIPELINES": 3,
        "WAREHOUSE": 3, "SQL": 3, "DASHBOARDING_BI": 2,
    },
    "DATA_ENGINEERING": {
        "ETL_PIPELINES": 5, "BIG_DATA": 4, "WAREHOUSE": 3, "DATA_QUALITY": 2,
        "PROGRAMMING": 2, "SQL": 2,
    },
    "SOFTWARE_ENGINEERING": {
        "SOFTWARE_BUILD": 6, "INFRASTRUCTURE": 4, "PROGRAMMING": 3, "ETL_PIPELINES": 1,
    },
}

#: Title words that nudge a function without deciding it. A title contributes at
#: most this much, so a strongly analytical JD under an odd title still wins.
FUNCTION_TITLE_HINTS: dict[str, re.Pattern[str]] = {
    "DATA_SCIENCE": re.compile(r"\b(data scien\w+|applied scientist|research scien\w+|statistician)\b", re.I),
    "MACHINE_LEARNING_ENGINEERING": re.compile(r"\b(machine learning|\bml\b|\bmle\b)\b", re.I),
    "APPLIED_AI_SOFTWARE": re.compile(r"\b(\bai\b|applied ai|artificial intelligence|genai)\b", re.I),
    "DATA_ANALYTICS": re.compile(r"\b(data analyst|business analyst|insights?|reporting analyst|analytics)\b", re.I),
    "PRODUCT_ANALYTICS": re.compile(r"\b(product analy\w+|growth analy\w+|user (?:research|analytics))\b", re.I),
    "DECISION_ANALYTICS": re.compile(r"\b(decision scien\w+|quantitative|risk|pricing|marketplace|strategy)\b", re.I),
    "ANALYTICS_ENGINEERING": re.compile(r"\b(analytics engineer|business intelligence|\bbi\b)\b", re.I),
    "DATA_ENGINEERING": re.compile(r"\b(data engineer|data platform|data infrastructure)\b", re.I),
    "SOFTWARE_ENGINEERING": re.compile(r"\b(software engineer|developer|backend|full[- ]stack)\b", re.I),
}

_TITLE_HINT_WEIGHT = 3

DOMAIN_SIGNALS: dict[str, re.Pattern[str]] = {
    "RISK": re.compile(r"\b(risk|fraud|underwriting|credit|default|chargeback)\b", re.I),
    "PRICING": re.compile(r"\b(pricing|price optimi[sz]ation|monetization|discount\w*)\b", re.I),
    "GROWTH": re.compile(r"\b(growth|acquisition|activation|referral|top[- ]of[- ]funnel)\b", re.I),
    "MARKETPLACE": re.compile(r"\b(marketplace|supply and demand|supply[- ]demand|two[- ]sided|matching)\b", re.I),
    "OPERATIONS": re.compile(r"\b(operations|logistics|fulfilment|fulfillment|throughput|capacity)\b", re.I),
    "STRATEGY": re.compile(r"\b(strategy|strategic|corporate development|market sizing)\b", re.I),
    "MEASUREMENT": re.compile(r"\b(measurement|attribution|incrementality|media mix)\b", re.I),
    "INSIGHTS": re.compile(r"\b(insights|voice of (?:the )?customer|market research)\b", re.I),
    "MARKETING": re.compile(r"\b(marketing|campaign|crm|lifecycle marketing|email)\b", re.I),
    "SUPPLY_CHAIN": re.compile(r"\b(supply chain|inventory|procurement|demand planning)\b", re.I),
    "HEALTHCARE": re.compile(r"\b(healthcare|clinical data|patient|claims|payer|provider|ehr|hipaa)\b", re.I),
    "FINANCE": re.compile(r"\b(finance|financial|revenue|payments?|banking|lending)\b", re.I),
    "TRUST_AND_SAFETY": re.compile(r"\b(trust and safety|abuse|moderation|integrity)\b", re.I),
    "PEOPLE": re.compile(r"\b(people analytics|workforce|talent analytics|employee)\b", re.I),
}


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FunctionalRoleProfile:
    """What this posting's work actually is.

    Consumed by screening, ranking, resume targeting and cover letters, so all
    four agree about what a job is instead of each deciding separately.
    """

    #: TITLE_CLEAR_RELEVANT | TITLE_AMBIGUOUS_REQUIRES_JD_ANALYSIS |
    #: TITLE_CLEAR_IRRELEVANT
    title_triage: str

    #: The matched TARGET function. Empty when the posting is not a target —
    #: everything that filters or ranks reads this.
    primary_function: str = ""
    #: What the work IS, target or not. A frontend posting is software
    #: engineering even though it is deprioritised, and saying so is the whole
    #: point of D-49: SOFTWARE_BUILD means "classify this as software", never
    #: "discard this". The "why this matches" panel reads this one, so it can
    #: say "Software Engineering — specialisation mismatch" rather than falling
    #: silent or, worse, saying "Data Analytics".
    classified_function: str = ""
    secondary_functions: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()

    primary_capabilities: tuple[str, ...] = ()
    secondary_capabilities: tuple[str, ...] = ()
    negative_function_signals: tuple[str, ...] = ()

    #: 0-100. How confident the function assignment is — driven by how much
    #: capability evidence the description actually carried.
    functional_match_confidence: int = 0
    #: True when the work is on target for an active function.
    is_functional_match: bool = False

    #: PRIMARY_TARGET | SECONDARY_TARGET | DEPRIORITIZED. A second axis, not a
    #: second lane: both target tiers live in FULL_TIME_2027 and both appear in
    #: the feed. Software engineering is a SECONDARY_TARGET — a real target that
    #: usually ranks behind a primary data/AI role at similar fit. D-49.
    target_priority: str = DEPRIORITIZED
    #: ALIGNED | NEUTRAL | MISMATCHED — for software roles only, whether the
    #: SPECIALISATION is one the evidence supports. Independent of seniority.
    specialization_fit: str = SPECIALIZATION_NEUTRAL
    #: The specialisations that pushed this to MISMATCHED, e.g. ("MOBILE",).
    specialization_signals: tuple[str, ...] = ()

    #: TITLE_ALIGNED (title says what the job is) | TITLE_UNDERSELLS (title
    #: hides relevant work) | TITLE_OVERSELLS (familiar title, work is not on
    #: target) | TITLE_UNCLEAR
    title_alignment: str = "TITLE_UNCLEAR"

    function_scores: dict[str, int] = field(default_factory=dict)
    #: Human-readable, for the "why this matches" panel. Never a formula.
    reasons: tuple[str, ...] = ()
    classifier_version: str = FUNCTIONAL_CLASSIFIER_VERSION

    @property
    def hidden_fit(self) -> bool:
        """Unusual title, relevant function.

        Deliberately NOT the same thing as a reach: this is about the TITLE
        hiding the work, and says nothing about seniority. A job can be a hidden
        fit and entry-compatible, or a hidden fit and a reach.
        """
        return self.is_functional_match and self.title_alignment == "TITLE_UNDERSELLS"

    def as_dict(self) -> dict[str, Any]:
        return {
            "title_triage": self.title_triage,
            "primary_function": self.primary_function,
            "classified_function": self.classified_function,
            "secondary_functions": list(self.secondary_functions),
            "domains": list(self.domains),
            "primary_capabilities": list(self.primary_capabilities),
            "secondary_capabilities": list(self.secondary_capabilities),
            "negative_function_signals": list(self.negative_function_signals),
            "functional_match_confidence": self.functional_match_confidence,
            "is_functional_match": self.is_functional_match,
            "target_priority": self.target_priority,
            "specialization_fit": self.specialization_fit,
            "specialization_signals": list(self.specialization_signals),
            "title_alignment": self.title_alignment,
            "hidden_fit": self.hidden_fit,
            "reasons": list(self.reasons),
            "classifier_version": self.classifier_version,
        }


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


class FunctionalClassifier:
    """Deterministic. No I/O, no model call, no Evidence Bank access."""

    def __init__(self, strategy: dict[str, Any] | None = None) -> None:
        self.strategy = strategy or load_yaml_config(STRATEGY_CONFIG)
        functions = self.strategy.get("target_functions") or []
        self._active_functions = {
            str(f.get("name")) for f in functions if f.get("active")
        }
        #: Function → PRIMARY_TARGET / SECONDARY_TARGET, from config rather than
        #: a literal here, so retiering one is a config change.
        self._function_priority = {
            str(f.get("name")): str(f.get("priority") or SECONDARY_TARGET)
            for f in functions
            if f.get("active")
        }

    # -- public ----------------------------------------------------------
    def classify(self, *, title: str, description: str) -> FunctionalRoleProfile:
        title_text = (title or "").strip()
        # The whole description matters here — capability evidence is often in
        # a "What you'll do" section well past any 2,000-character head.
        body = (description or "")[:20000]
        blob = f"{title_text}\n{body}"

        triage = self._triage(title_text)
        if triage == TITLE_CLEAR_IRRELEVANT:
            return FunctionalRoleProfile(
                title_triage=triage,
                reasons=(f"Title names a different occupation: {title_text[:70]!r}.",),
            )

        capabilities = self._capabilities(blob)
        # Negatives read the title too: "Solutions Architect" and "Vendor
        # Security Manager" announce the occupation in the title and describe it
        # only obliquely in the body.
        negatives = self._negatives(blob)
        domains = self._domains(blob)

        core = {name for name, _w in capabilities} & _CORE_CAPABILITIES
        decisive_negative = {n for n, _w in negatives} & _DECISIVE_NEGATIVES

        scores = self._function_scores(capabilities, title_text)
        primary, secondaries = self._rank_functions(scores)

        confidence = self._confidence(capabilities, core, negatives)
        reasons: list[str] = []

        # -- is this actually data work? ---------------------------------
        #
        # TWO core capabilities, not one. The first audit pass required one and
        # recovered 2,178 jobs including backend engineers, solutions architects
        # and a security manager — every JD in a tech company says "SQL"
        # somewhere. Two is the difference between a job that touches data and a
        # job that IS data work.
        # A software role is measured against SOFTWARE capabilities, not the
        # analytics core set. A service with a PostgreSQL database and a REST
        # API is squarely on the candidate's OpenText experience and contains no
        # "experimentation" or "predictive modelling" at all — measuring it
        # against those was why every backend posting failed the bar. D-49.
        present = {name for name, _w in capabilities}
        swe_core = present & _SWE_CORE_CAPABILITIES
        is_software = primary == "SOFTWARE_ENGINEERING"
        evidence = swe_core if is_software else core
        enough_evidence = len(evidence) >= MIN_CORE_CAPABILITIES

        specialization, spec_signals = self._specialization(blob, is_software, title_text)

        matched = enough_evidence and primary in self._active_functions

        # A mismatched specialisation is the honest headline reason for a
        # frontend or mobile posting, whether or not the capability count also
        # fell short. Saying "not enough software work" about an iOS role is
        # true but misleading — there is plenty of software work; it is the
        # wrong software.
        # Not gated on `is_software`: MISMATCHED only ever comes from a
        # software specialisation, and an "Android Engineer" whose body mentions
        # SQL and Python is still an Android role — it scored DATA_ANALYTICS and
        # passed until this stopped being conditional.
        if specialization == SPECIALIZATION_MISMATCHED:
            matched = False
            named_specs = ", ".join(spec_signals).replace("_", " ").lower()
            reasons.append(
                f"Software engineering, but the specialisation is {named_specs} — "
                "outside the backend, API, database and data-platform work the "
                "evidence covers."
            )
        elif not enough_evidence:
            noun = "software" if is_software else "hands-on data"
            reasons.append(
                f"The description does not describe enough {noun} work: "
                f"{len(evidence)} core capability of the {MIN_CORE_CAPABILITIES} needed."
            )
        elif primary not in self._active_functions:
            reasons.append(
                f"The work described is {primary.replace('_', ' ').lower()}, which is not "
                "an active target function for this search."
            )
        # A decisive negative names a different OCCUPATION. It does not get to
        # veto a posting whose title says "Data Scientist" and whose description
        # is full of hands-on data work — a data scientist at a fintech talks
        # about finance and sales all day, and that is the domain, not the job.
        # The audit caught this dropping a fintech's own Data Scientist posting.
        if decisive_negative and not (
            triage == TITLE_CLEAR_RELEVANT and len(core) >= MIN_CORE_CAPABILITIES
        ):
            matched = False
            reasons.append(
                "The description describes a different occupation: "
                + ", ".join(sorted(decisive_negative)).replace("_", " ").lower()
                + "."
            )
        elif decisive_negative:
            reasons.append(
                "The posting sits in a "
                + ", ".join(sorted(decisive_negative)).replace("_", " ").lower()
                + " domain, but the work described is data work."
            )
        elif negatives and sum(w for _n, w in negatives) >= 6 and len(core) < 2:
            matched = False
            reasons.append(
                "Non-analytical responsibilities dominate the description and the data "
                "work is incidental."
            )

        priority = DEPRIORITIZED
        if matched:
            priority = self._function_priority.get(primary, SECONDARY_TARGET)
            named = ", ".join(sorted(evidence)[:4]).replace("_", " ").lower()
            reasons.insert(0, f"The description asks for {named}.")

            # A mismatched specialisation has already set `matched` False
            # above; `classified_function` still records that this is software.
            if is_software:
                reasons.append(
                    "Software engineering is a secondary target: real, and usually "
                    "ranked behind a data or AI role at similar fit."
                )

        alignment = self._alignment(triage, matched, primary, title_text)
        if alignment == "TITLE_UNDERSELLS":
            reasons.append(
                f"The title {title_text[:60]!r} carries no obvious data keyword, but the "
                f"work described is {primary.replace('_', ' ').lower()}."
            )
        elif alignment == "TITLE_OVERSELLS":
            reasons.append(
                "The title looks like a target role, but the described work is not."
            )

        return FunctionalRoleProfile(
            title_triage=triage,
            primary_function=primary if matched else "",
            classified_function=primary,
            secondary_functions=tuple(secondaries) if matched else (),
            domains=tuple(domains),
            primary_capabilities=tuple(sorted(evidence)),
            secondary_capabilities=tuple(sorted(present - evidence)),
            negative_function_signals=tuple(sorted(n for n, _w in negatives)),
            functional_match_confidence=confidence,
            is_functional_match=matched,
            target_priority=priority,
            specialization_fit=specialization,
            specialization_signals=tuple(spec_signals),
            title_alignment=alignment,
            function_scores=scores,
            reasons=tuple(reasons),
        )

    # -- pieces ----------------------------------------------------------
    def _triage(self, title: str) -> str:
        """Only an unambiguously different occupation is rejected on its title."""
        if not title:
            return TITLE_AMBIGUOUS_REQUIRES_JD_ANALYSIS
        # A title naming another occupation is irrelevant UNLESS it also names
        # target work: "Sales Analytics Data Scientist" is a data role.
        if _TITLE_CLEARLY_IRRELEVANT.search(title) and not _TITLE_CLEARLY_RELEVANT.search(title):
            return TITLE_CLEAR_IRRELEVANT
        if _TITLE_CLEARLY_RELEVANT.search(title):
            return TITLE_CLEAR_RELEVANT
        return TITLE_AMBIGUOUS_REQUIRES_JD_ANALYSIS

    def _specialization(
        self, blob: str, is_software: bool, title: str = ""
    ) -> tuple[str, list[str]]:
        """Which software specialisation this is, and whether it fits.

        Only asked of software roles: a "mobile" mention in an analytics posting
        says nothing about the analytics. And a mismatched signal is overridden
        by a strong aligned one, because "Backend Engineer, iOS Platform Team"
        is backend work whatever the team is called — the JD has to be MOSTLY
        the mismatched specialisation, which is what counting both sides does.
        """
        # A mismatched specialisation named in an ENGINEERING title is decisive
        # on its own, whichever function won the scoring. "Android Engineer II
        # (Healthcare)" reached the active list because its body named Android
        # once and the two-occurrence rule needs two — but nobody titles a
        # backend role "Android Engineer".
        #
        # Gated on the title being an engineering title so that "Mobile
        # Marketing Analyst" is not caught by the word "mobile": that is a
        # domain, and domains never decide relevance.
        if _ENGINEERING_TITLE.search(title):
            in_title = [name for name, pattern in _TITLE_SPECIALIZATION if pattern.search(title)]
            if in_title:
                return SPECIALIZATION_MISMATCHED, in_title

        if not is_software:
            return SPECIALIZATION_NEUTRAL, []

        mismatched = [
            name for name, pattern in _SWE_MISMATCHED if len(pattern.findall(blob)) >= 2
        ]
        aligned_hits = len(_SWE_ALIGNED.findall(blob))

        if mismatched and aligned_hits < 4:
            return SPECIALIZATION_MISMATCHED, mismatched
        if aligned_hits >= 2:
            return SPECIALIZATION_ALIGNED, []
        return SPECIALIZATION_NEUTRAL, mismatched

    def _capabilities(self, blob: str) -> list[tuple[str, int]]:
        """A capability needs to be mentioned like a capability.

        Every SharkNinja posting — mechanical engineer, supply planner, product
        integrity engineer — carries the sentence "our people are expected to
        experiment boldly". One `search()` for "experiment" therefore handed
        EXPERIMENTATION to all of them, and the retrospective audit recovered a
        Mechanical Engineer I as DATA_ANALYTICS.

        So a family counts when it is mentioned TWICE, or once in a line that
        reads like a requirement. A job that actually involves SQL says so more
        than once; a culture paragraph says "experiment" and moves on.
        """
        found: list[tuple[str, int]] = []
        for name, weight, pattern in CAPABILITY_FAMILIES:
            matches = pattern.findall(blob)
            if len(matches) >= 2:
                found.append((name, weight))
                continue
            if not matches:
                continue
            match = pattern.search(blob)
            if not match:
                continue
            # Either the mention sits in a requirement phrase, or the thing
            # mentioned is a named technology — a stack list names each tool
            # once and is still a requirement.
            window = blob[max(0, match.start() - 120) : match.end() + 60]
            if _REQUIREMENT_CONTEXT.search(window) or _NAMED_TECHNOLOGY.fullmatch(
                match.group(0).strip()
            ):
                found.append((name, weight))
        return found

    def _negatives(self, body: str) -> list[tuple[str, int]]:
        return [(name, weight) for name, weight, pattern in NEGATIVE_FUNCTION_SIGNALS
                if pattern.search(body)]

    def _domains(self, blob: str) -> list[str]:
        hits = [(name, len(pattern.findall(blob))) for name, pattern in DOMAIN_SIGNALS.items()]
        # A single passing mention of "finance" does not make it a finance role.
        ranked = sorted((h for h in hits if h[1] >= 2), key=lambda h: -h[1])
        return [name for name, _count in ranked[:3]]

    def _function_scores(
        self, capabilities: list[tuple[str, int]], title: str
    ) -> dict[str, int]:
        present = dict(capabilities)
        scores: dict[str, int] = {}
        for function, weights in FUNCTION_SIGNALS.items():
            score = sum(weight for cap, weight in weights.items() if cap in present)
            hint = FUNCTION_TITLE_HINTS.get(function)
            # The title adds a fixed nudge and nothing more. A JD full of
            # experimentation and funnels outranks the word "Strategy".
            if hint and hint.search(title):
                score += _TITLE_HINT_WEIGHT
            if score:
                scores[function] = score
        return scores

    def _rank_functions(self, scores: dict[str, int]) -> tuple[str, list[str]]:
        if not scores:
            return "", []
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        primary = ranked[0][0]
        threshold = max(2, ranked[0][1] // 2)
        secondaries = [name for name, score in ranked[1:] if score >= threshold][:2]
        return primary, secondaries

    def _confidence(
        self,
        capabilities: list[tuple[str, int]],
        core: set[str],
        negatives: list[tuple[str, int]],
    ) -> int:
        """How much the description actually said, not how sure we feel."""
        evidence = sum(weight for _name, weight in capabilities)
        confidence = min(95, evidence * 4 + len(core) * 6)
        confidence -= sum(weight for _name, weight in negatives) * 4
        return max(0, min(100, confidence))

    def _alignment(self, triage: str, matched: bool, primary: str, title: str) -> str:
        if triage == TITLE_CLEAR_RELEVANT:
            return "TITLE_ALIGNED" if matched else "TITLE_OVERSELLS"
        if not matched:
            return "TITLE_UNCLEAR"
        # An ambiguous title over genuinely on-target work is the whole point.
        return "TITLE_UNDERSELLS"


_CLASSIFIER: FunctionalClassifier | None = None


def classify_function(title: str, description: str) -> FunctionalRoleProfile:
    """Module-level entry point with a cached classifier."""
    global _CLASSIFIER
    if _CLASSIFIER is None:
        _CLASSIFIER = FunctionalClassifier()
    return _CLASSIFIER.classify(title=title, description=description)


def reset_classifier_cache() -> None:
    global _CLASSIFIER
    _CLASSIFIER = None
