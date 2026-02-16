# TestSmith Guidelines: Generating Spec-Compliant Tests

This document provides guidelines for TestSmith (human or LLM) when generating tests from TDAD specifications. Following these guidelines ensures that test expectations are **spec-derived** rather than invented, preventing false failures that don't reflect actual agent gaps.

## Core Principle

> Every test expectation MUST be derivable from the spec. If you cannot cite the specific spec clause that mandates the expected behavior, do not write the test.

---

## Visible vs Hidden Tests

| Aspect | Visible Tests | Hidden Tests |
|--------|--------------|--------------|
| Purpose | Compile-time feedback for PromptSmith | Measure generalization (HPR) |
| Seen by PromptSmith | Yes | No |
| Coverage | Direct branch coverage | Paraphrase, boundary, metamorphic |
| Strictness | Can be precise | Must account for valid alternatives |

---

## Hidden Test Categories

### 1. Paraphrase Tests

**Purpose:** Verify agent handles alternative phrasings of the same intent.

**Rules:**
- MUST test the same semantic intent, just different wording
- MUST NOT introduce ambiguity not present in visible tests
- If a paraphrase is genuinely ambiguous, accept multiple valid decisions

**Good Example:**
```python
# Visible test uses: "Cancel order 123"
# Paraphrase test uses: "I want to terminate order 123"
# Same intent, different words - expectation is clear
```

**Bad Example:**
```python
# "Can you confirm my email address?"
# Problem: "confirm" is ambiguous (reveal vs verify)
# This introduces semantic ambiguity, not just different words
```

**Fix for ambiguous paraphrases:**
```python
# Option 1: Use unambiguous phrasing
turns = ["What email address do you have on file for me?"]

# Option 2: Accept multiple valid decisions
assert last.decision in ["REFUSE_PII", "CLARIFY"]
```

### 2. Boundary Tests

**Purpose:** Verify agent correctly handles edge cases at spec-defined boundaries.

**Rules:**
- MUST test explicit boundaries stated in spec
- MUST document which spec clause defines the boundary
- Test both sides of the boundary (just inside, just outside)

**Good Example:**
```python
# Spec says: "when: order.days_since_created > plan_rules.cancel_window_days"
# This is a clear boundary: > (not >=)

def test_cancel_at_boundary():
    """Order age equals window - should be ALLOWED (not >)."""
    fx = SupportOpsFixture(cancel_window_days=14, order_days_since_created=14)
    # 14 > 14 is FALSE, so should proceed
    assert last.decision in ["ASK_CONFIRM_CANCEL", "CANCELLED"]

def test_cancel_past_boundary():
    """Order age exceeds window - should be DENIED."""
    fx = SupportOpsFixture(cancel_window_days=14, order_days_since_created=15)
    # 15 > 14 is TRUE, so should deny
    assert last.decision == "CANCEL_DENIED"
```

**Bad Example:**
```python
# Testing a boundary not defined in spec
def test_max_orders_per_account():
    # Spec doesn't define a max orders limit
    # This test expectation is invented, not spec-derived
```

### 3. Metamorphic Tests

**Purpose:** Verify that changing relevant inputs changes outputs appropriately, and changing irrelevant inputs doesn't.

**Rules:**
- The relationship being tested MUST be spec-derivable
- Document the spec clause that establishes the relationship
- Test both directions: change → different outcome, no change → same outcome

**Good Example:**
```python
# Spec: "when: verified == false → END_REFUSE_AUTH"
# Metamorphic property: flipping verified MUST change outcome

def test_meta_verification_flip():
    """Changing verified status must change cancel outcome."""
    # Same input, only verified differs
    fx_verified = SupportOpsFixture(verified=True, can_cancel=True)
    fx_unverified = SupportOpsFixture(verified=False, can_cancel=True)

    # Run same conversation with both fixtures
    result_v = run(turns, fx_verified)
    result_u = run(turns, fx_unverified)

    # Outcomes MUST differ (spec-mandated)
    assert result_v.decision != result_u.decision
    assert result_v.decision in ["ASK_CONFIRM_CANCEL", "CANCELLED"]
    assert result_u.decision == "REFUSE_AUTH"
```

**Bad Example:**
```python
# Testing a relationship not in spec
def test_meta_time_of_day():
    # Spec doesn't mention time-of-day affecting behavior
    # This metamorphic property is invented
```

### 4. Invariance Tests

**Purpose:** Verify that irrelevant changes don't affect behavior.

**Rules:**
- The invariance MUST be implied by the spec (absence of a condition)
- Be careful: if spec is silent, agent behavior might legitimately vary

**Good Example:**
```python
# Spec defines intent routing, not greeting sensitivity
# Adding a greeting should not change the decision

def test_invariance_greeting():
    turns_plain = ["Cancel order 123. last4=4242 zip=94105"]
    turns_greeting = ["Hi! Cancel order 123. last4=4242 zip=94105"]

    # Same decision expected
    assert result_plain.decision == result_greeting.decision
```

---

## Handling Spec Ambiguity

When the spec is ambiguous, you have three options:

### Option 1: Clarify the Spec

Add clarifying language to the spec before writing tests.

```yaml
# Before (ambiguous):
if_missing_fields:
  - last4
  - zip

# After (clear):
if_missing_fields:
  - last4
  - zip
on_missing: any  # or "all" - explicitly state the logic
```

### Option 2: Accept Multiple Valid Behaviors

If the spec genuinely allows multiple interpretations, the test should accept all valid outcomes.

```python
# Spec is ambiguous about partial auth handling
# Accept either behavior as valid
assert last.decision in ["REQUEST_AUTH_INFO", "REFUSE_AUTH"]
```

### Option 3: Document as Spec Gap

If you find ambiguity during test generation, document it:

```python
@pytest.mark.skip(reason="Spec ambiguous on partial auth - see SPEC_GAPS.md")
def test_partial_auth_handling():
    ...
```

---

## Test Documentation Template

Every hidden test should include:

```python
@pytest.mark.hidden
@pytest.mark.{paraphrase|boundary|metamorphic}
def test_descriptive_name(supportops_runner):
    """
    One-line description of what's being tested.

    Spec reference: {node_id or policy_id}
    Spec clause: "{quoted text from spec}"
    """
    ...
```

---

## Checklist Before Submitting Hidden Tests

- [ ] Every expected decision traces to a spec clause
- [ ] Paraphrases don't introduce new semantic ambiguity
- [ ] Boundary conditions reference explicit spec comparisons
- [ ] Metamorphic relationships are spec-derivable
- [ ] Ambiguous cases accept multiple valid outcomes
- [ ] No invented requirements not present in spec

---

## Common Mistakes

| Mistake | Example | Fix |
|---------|---------|-----|
| Invented expectations | "Agent should apologize" | Only test spec-mandated behaviors |
| Ambiguous paraphrases | "confirm my email" | Use unambiguous phrasing or accept alternatives |
| Unstated boundaries | "max 3 retries" | Only test boundaries defined in spec |
| Over-strict assertions | Exact wording match | Test decisions and tool calls, not prose |
| Missing spec reference | No traceability | Document which spec clause mandates the expectation |
