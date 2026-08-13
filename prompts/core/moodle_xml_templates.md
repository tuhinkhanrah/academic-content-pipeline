# Moodle XML Reference Templates (Compact)

Skeleton structures for each question type. Reuse these tag shapes; fill content per question. Feedback text should follow the Feedback Rules in the core rules doc, not the placeholder text below.

## 1. File Wrapper

<?xml version="1.0" encoding="UTF-8"?>
<quiz>
  <!-- Individual <question> nodes go here -->
</quiz>

## 2. Single-Response MCQ (`single="true"`)

Applies to standard direct-answer, "best answer", and negative/"NOT"/"EXCEPT" phrasing — same skeleton, only questiontext wording changes.

<question type="multichoice">
  <name><text>NAME</text></name>
  <questiontext format="html"><text><![CDATA[<p>QUESTION</p>]]></text></questiontext>
  <generalfeedback format="html"><text><![CDATA[<p>EXPLANATION</p>]]></text></generalfeedback>
  <defaultgrade>1.0000000</defaultgrade>
  <penalty>0.3333333</penalty>
  <hidden>0</hidden>
  <single>true</single>
  <shuffleanswers>true</shuffleanswers>
  <answernumbering>abc</answernumbering>
  <answer fraction="100" format="html"><text><![CDATA[<p>CORRECT_OPTION</p>]]></text></answer>
  <answer fraction="0" format="html"><text><![CDATA[<p>DISTRACTOR</p>]]></text></answer>
  <tags>
    <tag><text>standard:VALUE</text></tag>
  </tags>
</question>

## 3. Multi-Response MCQ (`single="false"`)

Same skeleton as above with `<single>false</single>`. Split `fraction="100"` evenly across all correct options (e.g. 3 correct -> 33.33333 each); give incorrect options a negative fraction (e.g. -50).

## 4. Statement-Based MCQ (2 or 3+ statements)

Same skeleton as Section 2. `questiontext` lists statements (`Statement I/II` or an `<ol>`); options are combination phrases (e.g. "1 and 2 only", "Both Statement I and Statement II are correct"). Set `<shuffleanswers>false</shuffleanswers>` since combination options are position-dependent.

## 5. Assertion–Reasoning MCQ

Fixed 4-option pattern — always use exactly these option texts in this order:

<question type="multichoice">
  <name><text>NAME</text></name>
  <questiontext format="html"><text><![CDATA[
    <p><strong>Assertion (A):</strong> ASSERTION_TEXT</p>
    <p><strong>Reason (R):</strong> REASON_TEXT</p>
  ]]></text></questiontext>
  <generalfeedback format="html"><text><![CDATA[<p>EXPLANATION</p>]]></text></generalfeedback>
  <defaultgrade>1.0000000</defaultgrade>
  <penalty>0.3333333</penalty>
  <single>true</single>
  <shuffleanswers>false</shuffleanswers>
  <answernumbering>abc</answernumbering>
  <answer fraction="100" format="html"><text><![CDATA[<p>Both (A) and (R) are correct and (R) is the correct explanation of (A).</p>]]></text></answer>
  <answer fraction="0" format="html"><text><![CDATA[<p>Both (A) and (R) are correct but (R) is NOT the correct explanation of (A).</p>]]></text></answer>
  <answer fraction="0" format="html"><text><![CDATA[<p>(A) is correct but (R) is incorrect.</p>]]></text></answer>
  <answer fraction="0" format="html"><text><![CDATA[<p>(A) is incorrect but (R) is correct.</p>]]></text></answer>
  <tags><tag><text>typology:assertion-reason</text></tag></tags>
</question>

## 6. Matching Questions

### 6.1 Native (`type="matching"`)

<question type="matching">
  <name><text>NAME</text></name>
  <questiontext format="html"><text><![CDATA[<p>Match Column A with Column B:</p>]]></text></questiontext>
  <generalfeedback format="html"><text><![CDATA[<p>EXPLANATION</p>]]></text></generalfeedback>
  <defaultgrade>1.0000000</defaultgrade>
  <shuffleanswers>true</shuffleanswers>
  <subquestion format="html"><text><![CDATA[<p>ITEM_A</p>]]></text><answer><text>MATCH_B</text></answer></subquestion>
  <tags><tag><text>typology:matching-native</text></tag></tags>
</question>

### 6.2 Via Standard MCQ (`type="multichoice"`)

Use the Section 2 skeleton; put an HTML `<table>` of Column I/II pairs in `questiontext`, and combination strings (e.g. "1-B, 2-C, 3-A") as the options. Tag `typology:matching-mcq`.

## 7. Numerical Question (`type="numerical"`)

<question type="numerical">
  <name><text>NAME</text></name>
  <questiontext format="html"><text><![CDATA[<p>QUESTION</p>]]></text></questiontext>
  <generalfeedback format="html"><text><![CDATA[<p>EXPLANATION</p>]]></text></generalfeedback>
  <defaultgrade>1.0000000</defaultgrade>
  <penalty>0.3333333</penalty>
  <answer fraction="100" format="moodle_auto_format">
    <text>NUMERIC_ANSWER</text>
    <tolerance>0.5</tolerance>
  </answer>
  <unitgradingtype>0</unitgradingtype>
  <unitpenalty>0.1000000</unitpenalty>
  <showunits>3</showunits>
  <tags><tag><text>standard:VALUE</text></tag></tags>
</question>
