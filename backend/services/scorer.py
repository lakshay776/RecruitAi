"""
services/scorer.py
------------------
Feature 3: Hybrid scoring of a single candidate against a job description.

Design principle: **let math do math, let the LLM do judgment.**

An LLM "score" is a guess dressed up as a number — unreliable as a precise
metric. So the objective dimensions are computed deterministically and the LLM
is used only where real judgment is needed:

  COMPUTED IN PYTHON (auditable, reproducible):
    - hard_skills      (35%) — semantic skill coverage (skill_matcher)
    - must_have        (30%) — semantic coverage of non-negotiables
    - experience_fit   (15%) — numeric comparison of years
    - domain_knowledge (10%) — semantic coverage of domain overlap

  JUDGED BY THE LLM (genuinely subjective):
    - soft_skills      (10%) — interpersonal alignment inferred from the CV
    - explanation, gaps, interview_questions — the human-readable output

Because 4 of the 5 dimensions don't depend on the LLM, scoring still produces a
meaningful result even if the LLM call fails.
"""

import logging
from models.cv_models import ParsedCV
from models.jd_models import ParsedJD
from models.result_models import CandidateResult, ScoreBreakdown
from core.groq_client import chat_json
from services.skill_matcher import match_coverage

logger = logging.getLogger(__name__)

# ─── Scoring weights ──────────────────────────────────────────────────────────
# must_have is NOT an additive term — non-negotiables are a *gate*, not a bonus
# (see _must_have_gate). These four additive weights sum to 1.0 and produce the
# "base" score, which the gate then scales.
_WEIGHTS = {
    "hard_skills":      0.50,
    "experience_fit":   0.20,
    "soft_skills":      0.15,
    "domain_knowledge": 0.15,
}

# Failing every must-have multiplies the base score by this floor (i.e. a
# candidate who meets none of the non-negotiables loses half their score).
# Meeting all of them leaves the score untouched (gate = 1.0).
_MUST_HAVE_GATE_FLOOR = 0.5

# Stricter cutoff for must-have matching than for general skills — a
# non-negotiable should only count as met on a confident match.
_MUST_MATCH_THRESHOLD = 0.68

# Fallback "years required" inferred from the JD's seniority level when the JD
# states no explicit number, so experience still discriminates.
_LEVEL_YEARS = {
    "intern": 0, "entry": 0, "junior": 1,
    "mid-level": 3, "mid": 3, "intermediate": 3,
    "senior": 6, "lead": 8, "staff": 8, "principal": 10,
}

# ─── Prompt templates (LLM judges ONLY soft skills + narrative) ─────────────────

_SYSTEM_PROMPT = """\
You are a senior technical recruiter. The objective fit scores (hard skills,
must-haves, experience, domain) have ALREADY been computed from the data and are
given to you. Do NOT recompute them.

Your job is ONLY to:
  1. Rate soft-skill alignment 0-100, inferred from the candidate's profile.
  2. Write a 2-3 sentence explanation of the candidate's overall fit, referencing
     the computed scores and any missing requirements.
  3. List concrete gaps (missing/weak areas).
  4. Suggest exactly 3 interview questions that probe the gaps or verify claims.

Return EXACTLY this JSON — no extra keys, no markdown, no explanation:

{
  "soft_skills": <0-100>,
  "explanation": "<2-3 sentences>",
  "gaps": ["<gap>", ...],
  "interview_questions": ["<q1>", "<q2>", "<q3>"]
}
"""

_USER_PROMPT_TEMPLATE = """\
=== JOB DESCRIPTION ===
Job Title: {job_title}
Experience Required: {experience_years} years ({experience_level})
Soft Skills Wanted: {soft_skills}
Hard Skills Required: {hard_skills}
Must Have: {must_have}

=== CANDIDATE PROFILE ===
Name: {name}
Skills: {skills}
Experience: {experience_years_cv} years
Education: {education}
Career Trajectory: {career_trajectory}
Domain Experience: {domain_experience}

=== COMPUTED FIT SCORES (already calculated — for your reference) ===
Hard skills coverage: {hard_score}/100  (missing: {missing_hard})
Must-have coverage:   {must_score}/100  (missing: {missing_must})
Experience fit:       {exp_score}/100
Domain knowledge:     {domain_score}/100

Now assess soft-skill alignment and write the explanation, gaps, and questions.
"""


# ─── Deterministic sub-scorers ──────────────────────────────────────────────────

def _experience_fit(cv_years, jd_years, jd_level: str = "") -> float:
    """
    Numeric experience match.

    The required years come from the JD's explicit number, or — when it states
    none — are inferred from the seniority level so experience still
    discriminates (an "intern" JD and a "senior" JD shouldn't score everyone the
    same). Only when neither is available do we fall back to a neutral score.

    - Candidate meets/exceeds requirement → 100.
    - Candidate under requirement         → proportional (e.g. 1/6 yrs → 17).
    - Requirement unknown, years known     → 65 (mild neutral).
    - Requirement unknown, years unknown   → 50.
    """
    required = jd_years
    if not required or required <= 0:
        required = _LEVEL_YEARS.get((jd_level or "").strip().lower())

    if required is None:  # no number AND unrecognised level → can't judge
        return 65.0 if cv_years else 50.0
    if required <= 0:  # genuinely an entry-level / intern role
        return 100.0
    if cv_years is None:
        return 40.0  # a real requirement exists but the CV gives no years
    if cv_years >= required:
        return 100.0
    return round(max(0.0, 100.0 * float(cv_years) / float(required)), 1)


def _must_have_gate(must_score: float) -> float:
    """
    Turn must-have coverage into a multiplicative gate on the final score.

    Non-negotiables aren't worth "bonus points" — failing them should drag the
    whole score down. Meeting all → 1.0 (no penalty); meeting none → the floor.
    """
    return _MUST_HAVE_GATE_FLOOR + (1 - _MUST_HAVE_GATE_FLOOR) * (must_score / 100.0)


def _clamp(val, default=0.0) -> float:
    try:
        return max(0.0, min(100.0, float(val)))
    except (TypeError, ValueError):
        return float(default)


# ─── Public API ───────────────────────────────────────────────────────────────

async def score_candidate(cv: ParsedCV, jd: ParsedJD, rank: int = 0) -> CandidateResult:
    """
    Score a single candidate against the job description.

    Objective dimensions are computed deterministically; soft skills and the
    narrative come from the LLM. Returns a fully-populated CandidateResult.
    """
    logger.info("Scoring candidate '%s' (%s)...", cv.name or cv.filename, cv.candidate_id)

    # ── 1. Deterministic dimensions ──────────────────────────────────────────
    cv_domain_pool = (cv.domain_experience or []) + (cv.skills or [])

    hard_score, _, missing_hard = match_coverage(jd.hard_skills, cv.skills)
    # Must-haves match against SKILLS ONLY (not education/domain prose) and use a
    # stricter threshold — a non-negotiable shouldn't be satisfied by a vague hit.
    must_score, _, missing_must = match_coverage(
        jd.must_have, cv.skills, threshold=_MUST_MATCH_THRESHOLD
    )
    domain_score, _, _ = match_coverage(jd.domain_knowledge, cv_domain_pool)
    experience_fit = _experience_fit(
        cv.experience_years, jd.experience_years, jd.experience_level
    )

    # ── 2. LLM judgment: soft skills + narrative ─────────────────────────────
    user_prompt = _USER_PROMPT_TEMPLATE.format(
        job_title=jd.job_title,
        experience_years=jd.experience_years or "Not specified",
        experience_level=jd.experience_level,
        soft_skills=", ".join(jd.soft_skills) or "None specified",
        hard_skills=", ".join(jd.hard_skills) or "None specified",
        must_have=", ".join(jd.must_have) or "None specified",
        name=cv.name or cv.filename,
        skills=", ".join(cv.skills) or "Not listed",
        experience_years_cv=cv.experience_years or "Unknown",
        education=cv.education or "Not listed",
        career_trajectory=cv.career_trajectory or "Not listed",
        domain_experience=", ".join(cv.domain_experience) or "Not listed",
        hard_score=round(hard_score),
        must_score=round(must_score),
        exp_score=round(experience_fit),
        domain_score=round(domain_score),
        missing_hard=", ".join(missing_hard) or "none",
        missing_must=", ".join(missing_must) or "none",
    )

    try:
        llm = await chat_json(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.2,
            max_tokens=768,
        )
        soft_skills = _clamp(llm.get("soft_skills"), default=50)
        explanation = llm.get("explanation", "")
        gaps = llm.get("gaps", [])
        interview_questions = llm.get("interview_questions", [])
    except ValueError as exc:
        # LLM failed — fall back gracefully. The 4 computed dimensions still hold.
        logger.warning("LLM judgment failed for '%s': %s — using computed scores only",
                       cv.filename, exc)
        soft_skills = 50.0  # neutral; we have no signal
        explanation = (
            f"Computed fit: hard skills {round(hard_score)}/100, "
            f"must-haves {round(must_score)}/100, experience {round(experience_fit)}/100. "
            "(Narrative unavailable — LLM judgment step failed.)"
        )
        gaps = (missing_must + missing_hard) or ["Unable to evaluate qualitative fit"]
        interview_questions = []

    # ── 3. Weighted total, then must-have gate (deterministic) ───────────────
    # Base score from the four additive dimensions...
    base = (
        hard_score       * _WEIGHTS["hard_skills"] +
        soft_skills      * _WEIGHTS["soft_skills"] +
        experience_fit   * _WEIGHTS["experience_fit"] +
        domain_score     * _WEIGHTS["domain_knowledge"]
    )
    # ...then scale by how many non-negotiables are met. Missing must-haves drag
    # the whole score down instead of being hidden behind strong other areas.
    total = base * _must_have_gate(must_score)

    breakdown = ScoreBreakdown(
        hard_skills=hard_score,
        soft_skills=soft_skills,
        must_have=must_score,
        experience_fit=experience_fit,
        domain_knowledge=domain_score,
        total=round(total, 1),
    )

    result = CandidateResult(
        candidate_id=cv.candidate_id,
        rank=rank,
        name=cv.name or cv.filename,
        filename=cv.filename,
        email=cv.email or "",
        phone=cv.phone or "",
        score_breakdown=breakdown,
        explanation=explanation,
        gaps=gaps,
        interview_questions=interview_questions,
    )

    logger.info(
        "Scored '%s' → total=%.1f (hard=%.0f, must=%.0f, exp=%.0f, soft=%.0f, domain=%.0f)",
        result.name, breakdown.total, hard_score, must_score, experience_fit,
        soft_skills, domain_score,
    )

    return result
