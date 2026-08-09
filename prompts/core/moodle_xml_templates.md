# Complete Moodle XML Question Bank Templates: Comprehensive Guide & Reference

This document provides ready-to-import **Moodle XML templates** for all major question types, including single-response MCQs, multi-response MCQs, statement-based MCQs, native matching, HTML table matching, and numerical questions.

---

## 1. Moodle XML File Structure Overview

A Moodle XML question bank file is wrapped in `<quiz>` tags. Categories can be defined inside the XML file using a special category question type.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<quiz>

  <!-- Category Definition -->
  <question type="category">
    <category>
      <text>$course$/top/MCQ Taxonomy Templates</text>
    </category>
    <info format="html">
      <text><![CDATA[<p>Category for importing all standard and complex question formats.</p>]]></text>
    </info>
  </question>

  <!-- Individual Questions Go Here -->

</quiz>
```

---

## 2. Single-Response MCQs (`single="true"`)

### 2.1 Standard Direct Answer
```xml
<question type="multichoice">
  <name>
    <text>SR_01_Standard_Direct_Answer</text>
  </name>
  <questiontext format="html">
    <text><![CDATA[
      <p>What is the chemical element represented by the gold bar in the image below?</p>
    ]]></text>
  </questiontext>
  <generalfeedback format="html">
    <text><![CDATA[
      <p>Gold's chemical symbol is <strong>Au</strong>, derived from the Latin word <em>aurum</em>.</p>
    ]]></text>
  </generalfeedback>
  <defaultgrade>1.0000000</defaultgrade>
  <penalty>0.3333333</penalty>
  <hidden>0</hidden>
  <single>true</single>
  <shuffleanswers>true</shuffleanswers>
  <answernumbering>abc</answernumbering>
  <answer fraction="0" format="html">
    <text><![CDATA[<p>Ag</p>]]></text>
    <feedback format="html">
      <text><![CDATA[
        <p>Incorrect. Ag stands for Silver (Argentum).</p>
      ]]></text>
    </feedback>
  </answer>
  <answer fraction="100" format="html">
    <text><![CDATA[<p>Au</p>]]></text>
    <feedback format="html">
      <text><![CDATA[
        <p>Correct! Au is Gold.</p>
      ]]></text>
    </feedback>
  </answer>
  <answer fraction="0" format="html">
    <text><![CDATA[<p>Pb</p>]]></text>
    <feedback format="html">
      <text><![CDATA[<p>Pb stands for Lead (Plumbum).</p>]]></text>
    </feedback>
  </answer>
  <answer fraction="0" format="html">
    <text><![CDATA[<p>Fe</p>]]></text>
    <feedback format="html">
      <text><![CDATA[<p>Fe stands for Iron (Ferrum).</p>]]></text>
    </feedback>
  </answer>
  <tags>
    <tag><text>year:2026</text></tag>
    <tag><text>subject:chemistry</text></tag>
    <tag><text>difficulty:easy</text></tag>
  </tags>
</question>
```

---

### 2.2 "Best Answer" Format
```xml
<question type="multichoice">
  <name>
    <text>SR_02_Best_Answer_Format</text>
  </name>
  <questiontext format="html">
    <text><![CDATA[
      <p>A patient presents with sudden severe chest pain radiating to the left jaw.</p>
      <p>What is the <strong>MOST APPROPRIATE</strong> immediate initial action?</p>
    ]]></text>
  </questiontext>
  <generalfeedback format="html">
    <text><![CDATA[
      <p>Activating emergency medical services is the primary action.</p>
    ]]></text>
  </generalfeedback>
  <defaultgrade>1.0000000</defaultgrade>
  <single>true</single>
  <shuffleanswers>true</shuffleanswers>
  <answernumbering>abc</answernumbering>
  <answer fraction="100" format="html">
    <text><![CDATA[<p>Activate emergency medical services immediately and assess vitals</p>]]></text>
    <feedback format="html">
      <text><![CDATA[<p>Best action! Life safety comes first.</p>]]></text>
    </feedback>
  </answer>
  <answer fraction="33.33333" format="html">
    <text><![CDATA[<p>Obtain a 12-lead ECG</p>]]></text>
    <feedback format="html">
      <text><![CDATA[<p>Important diagnostic step, but EMS activation must happen concurrently/first.</p>]]></text>
    </feedback>
  </answer>
  <answer fraction="0" format="html">
    <text><![CDATA[<p>Administer oral pain relievers and monitor for 30 minutes</p>]]></text>
    <feedback format="html">
      <text><![CDATA[<p>Incorrect. Delays critical life-saving care.</p>]]></text>
    </feedback>
  </answer>
  <tags>
    <tag><text>year:2026</text></tag>
    <tag><text>subject:medicine</text></tag>
  </tags>  
</question>
```

---

### 2.3 Negative / Reversal Type ("NOT" / "EXCEPT")
```xml
<question type="multichoice">
  <name>
    <text>SR_03_Negative_Reversal_Type</text>
  </name>
  <questiontext format="html">
    <text><![CDATA[<p>Which of the following is <strong>NOT</strong> an essential amino acid in human nutrition?</p>]]></text>
  </questiontext>
  <generalfeedback format="html">
    <text><![CDATA[
      <p>Alanine is synthesized endogenously, making it non-essential.</p>
    ]]></text>
  </generalfeedback>
  <defaultgrade>1.0000000</defaultgrade>
  <single>true</single>
  <shuffleanswers>true</shuffleanswers>
  <answernumbering>abc</answernumbering>
  <answer fraction="0" format="html">
    <text><![CDATA[<p>Lysine</p>]]></text>
    <feedback format="html"><text><![CDATA[<p>Lysine IS essential.</p>]]></text></feedback>
  </answer>
  <answer fraction="100" format="html">
    <text><![CDATA[<p>Alanine</p>]]></text>
    <feedback format="html">
      <text><![CDATA[
        <p>Correct! Alanine is non-essential.</p>
        <p><img src="@@PLUGINFILE@@/check.png" alt="Correct" width="40" height="40" /></p>
      ]]></text>
    </feedback>
  </answer>
  <answer fraction="0" format="html">
    <text><![CDATA[<p>Valine</p>]]></text>
    <feedback format="html"><text><![CDATA[<p>Valine IS essential.</p>]]></text></feedback>
  </answer>
  <tags>
    <tag><text>year:2026</text></tag>
    <tag><text>subject:biochemistry</text></tag>
  </tags>  
</question>
```

---

## 3. Multi-Response MCQs (`single="false"`)

### 3.1 Select All That Apply (SATA)
```xml
<question type="multichoice">
  <name>
    <text>MR_01_Select_All_That_Apply</text>
  </name>
  <questiontext format="html">
    <text><![CDATA[
      <p>Which of the following numbers shown on the board are <strong>prime numbers</strong>? <em>(Select all that apply)</em></p>
    ]]></text>
  </questiontext>
  <generalfeedback format="html">
    <text><![CDATA[<p>2, 7, and 11 are prime numbers. 4 and 9 are composite.</p>]]></text>
  </generalfeedback>
  <defaultgrade>1.0000000</defaultgrade>
  <single>false</single>
  <shuffleanswers>true</shuffleanswers>
  <answernumbering>abc</answernumbering>
  <answer fraction="33.33333" format="html">
    <text><![CDATA[<p>2</p>]]></text>
    <feedback format="html"><text><![CDATA[<p>Correct! 2 is prime.</p>]]></text></feedback>
  </answer>
  <answer fraction="33.33333" format="html">
    <text><![CDATA[<p>7</p>]]></text>
    <feedback format="html"><text><![CDATA[<p>Correct! 7 is prime.</p>]]></text></feedback>
  </answer>
  <answer fraction="33.33333" format="html">
    <text><![CDATA[<p>11</p>]]></text>
    <feedback format="html"><text><![CDATA[<p>Correct! 11 is prime.</p>]]></text></feedback>
  </answer>
  <answer fraction="-50" format="html">
    <text><![CDATA[<p>4</p>]]></text>
    <feedback format="html"><text><![CDATA[<p>Incorrect. 4 is divisible by 2.</p>]]></text></feedback>
  </answer>
  <answer fraction="-50" format="html">
    <text><![CDATA[<p>9</p>]]></text>
    <feedback format="html"><text><![CDATA[<p>Incorrect. 9 is divisible by 3.</p>]]></text></feedback>
  </answer>
  <tags>
    <tag><text>year:2026</text></tag>
    <tag><text>subject:math</text></tag>
  </tags>  
</question>
```

---

## 4. Statement-Based MCQs

### 4.1 Dual Statement (Statement I & Statement II)
```xml
<question type="multichoice">
  <name>
    <text>ST_01_Dual_Statement</text>
  </name>
  <questiontext format="html">
    <text><![CDATA[
      <p>Consider the following two statements:</p>
      <p><strong>Statement I:</strong> Light travels faster in glass than in air.</p>
      <p><strong>Statement II:</strong> The refractive index of glass is higher than that of air.</p>
      <p>Which one of the following is correct?</p>
    ]]></text>
  </questiontext>
  <generalfeedback format="html">
    <text><![CDATA[<p>Statement I is incorrect because light travels slower in glass ($v = c/n$). Statement II is correct.</p>]]></text>
  </generalfeedback>
  <defaultgrade>1.0000000</defaultgrade>
  <single>true</single>
  <shuffleanswers>false</shuffleanswers>
  <answernumbering>abc</answernumbering>
  <answer fraction="0" format="html">
    <text><![CDATA[<p>Both Statement I and Statement II are correct.</p>]]></text>
    <feedback format="html"><text><![CDATA[]]></text></feedback>
  </answer>
  <answer fraction="0" format="html">
    <text><![CDATA[<p>Both Statement I and Statement II are incorrect.</p>]]></text>
    <feedback format="html"><text><![CDATA[]]></text></feedback>
  </answer>
  <answer fraction="0" format="html">
    <text><![CDATA[<p>Statement I is correct, but Statement II is incorrect.</p>]]></text>
    <feedback format="html"><text><![CDATA[]]></text></feedback>
  </answer>
  <answer fraction="100" format="html">
    <text><![CDATA[<p>Statement I is incorrect, but Statement II is correct.</p>]]></text>
    <feedback format="html">
      <text><![CDATA[
        <p>Correct identification!</p>
        <p><img src="@@PLUGINFILE@@/explanation_optics.png" alt="Optics Chart" width="200" height="100" /></p>
      ]]></text>
    </feedback>
  </answer>
  <tags>
    <tag><text>year:2026</text></tag>
    <tag><text>subject:physics</text></tag>
  </tags>  
</question>
```

---

### 4.2 Multi-Statement Combination (3+ Statements)
```xml
<question type="multichoice">
  <name>
    <text>ST_02_Multi_Statement_3_Plus</text>
  </name>
  <questiontext format="html">
    <text><![CDATA[
      <p>With reference to Photosynthesis, consider the following statements:</p>
      <ol>
        <li>Oxygen is released as a byproduct during the light-dependent reactions.</li>
        <li>Carbon dioxide is fixed into carbohydrates during the Calvin cycle.</li>
        <li>Chlorophyll pigments are located in the matrix of mitochondria.</li>
      </ol>
      <p>Which of the statements given above is/are <strong>correct</strong>?</p>
    ]]></text>
  </questiontext>
  <generalfeedback format="html">
    <text><![CDATA[
      <p>Statements 1 and 2 are correct. Statement 3 is false because chlorophyll is located in thylakoid membranes.</p>
    ]]></text>
  </generalfeedback>
  <defaultgrade>1.0000000</defaultgrade>
  <single>true</single>
  <shuffleanswers>false</shuffleanswers>
  <answernumbering>abc</answernumbering>
  <answer fraction="0" format="html">
    <text><![CDATA[<p>1 only</p>]]></text>
    <feedback format="html"><text><![CDATA[]]></text></feedback>
  </answer>
  <answer fraction="100" format="html">
    <text><![CDATA[<p>1 and 2 only</p>]]></text>
    <feedback format="html">
      <text><![CDATA[<p>Correct!</p>]]></text>
    </feedback>
  </answer>
  <answer fraction="0" format="html">
    <text><![CDATA[<p>2 and 3 only</p>]]></text>
    <feedback format="html"><text><![CDATA[]]></text></feedback>
  </answer>
  <answer fraction="0" format="html">
    <text><![CDATA[<p>1, 2, and 3</p>]]></text>
    <feedback format="html"><text><![CDATA[]]></text></feedback>
  </answer>
  <tags>
    <tag><text>year:2026</text></tag>
    <tag><text>subject:biology</text></tag>
  </tags>  
</question>
```

---


## 5. Assertion–Reasoning MCQs

Assertion–Reasoning questions present two factual statements—an **Assertion (A)** and a **Reason (R)**—and require evaluating the truth of each statement as well as whether Statement R correctly explains Statement A.

```xml
<question type="multichoice">
  <name>
    <text>AR_01_Assertion_Reasoning</text>
  </name>
  <questiontext format="html">
    <text><![CDATA[
      <p>Given below are two statements: one is labelled as <strong>Assertion (A)</strong> and the other is labelled as <strong>Reason (R)</strong>.</p>
      <p><strong>Assertion (A):</strong> Boiling point of water decreases at higher altitudes.</p>
      <p><strong>Reason (R):</strong> Atmospheric pressure decreases with an increase in altitude.</p>
      <p>In light of the above statements, choose the most appropriate answer from the options given below:</p>
    ]]></text>
  </questiontext>
  <generalfeedback format="html">
    <text><![CDATA[
      <p>Water boils when its vapor pressure equals atmospheric pressure. At higher altitudes, atmospheric pressure is reduced, allowing water to boil at a lower temperature. Therefore, both (A) and (R) are true, and (R) is the correct explanation of (A).</p>
    ]]></text>
  </generalfeedback>
  <defaultgrade>1.0000000</defaultgrade>
  <penalty>0.3333333</penalty>
  <hidden>0</hidden>
  <single>true</single>
  <shuffleanswers>false</shuffleanswers>
  <answernumbering>abc</answernumbering>
  <!-- Option 1 (Correct) -->
  <answer fraction="100" format="html">
    <text><![CDATA[<p>Both (A) and (R) are correct and (R) is the correct explanation of (A).</p>]]></text>
    <feedback format="html">
      <text><![CDATA[
        <p>Correct! Reduced atmospheric pressure directly causes the decrease in boiling point.</p>
      ]]></text>
    </feedback>
  </answer>
  <!-- Option 2 -->
  <answer fraction="0" format="html">
    <text><![CDATA[<p>Both (A) and (R) are correct but (R) is NOT the correct explanation of (A).</p>]]></text>
    <feedback format="html">
      <text><![CDATA[<p>Incorrect. (R) is indeed the direct cause of (A).</p>]]></text>
    </feedback>
  </answer>
  <!-- Option 3 -->
  <answer fraction="0" format="html">
    <text><![CDATA[<p>(A) is correct but (R) is incorrect.</p>]]></text>
    <feedback format="html">
      <text><![CDATA[<p>Incorrect. Atmospheric pressure does decrease with altitude.</p>]]></text>
    </feedback>
  </answer>
  <!-- Option 4 -->
  <answer fraction="0" format="html">
    <text><![CDATA[<p>(A) is incorrect but (R) is correct.</p>]]></text>
    <feedback format="html">
      <text><![CDATA[<p>Incorrect. Boiling point actually decreases at higher altitudes.</p>]]></text>
    </feedback>
  </answer>
  <!-- Tags Container (Supports multiple <tag> elements) -->
  <tags>
    <tag><text>year:2026</text></tag>
    <tag><text>subject:physics</text></tag>
    <tag><text>topic:thermodynamics</text></tag>
    <tag><text>typology:assertion-reason</text></tag>
    <tag><text>difficulty:medium</text></tag>
  </tags>
</question>

---

## 5. Matching & Relational Questions

### 5.1 Native Moodle Matching Format (`type="matching"`)
```xml
<question type="matching">
  <name>
    <text>MA_01_Native_Matching</text>
  </name>
  <questiontext format="html">
    <text><![CDATA[
      <p>Match each scientist in Column A with their breakthrough discovery in Column B:</p>
      <p><img src="@@PLUGINFILE@@/scientists_banner.png" alt="Scientists Diagram" width="280" height="120" /></p>
    ]]></text>
  </questiontext>
  <generalfeedback format="html">
    <text><![CDATA[
      <p>Newton formulated the Laws of Motion; Einstein developed Relativity; Fleming discovered Penicillin.</p>
    ]]></text>
  </generalfeedback>
  <defaultgrade>1.0000000</defaultgrade>
  <penalty>0.3333333</penalty>
  <hidden>0</hidden>
  <shuffleanswers>true</shuffleanswers>
  <subquestion format="html">
    <text><![CDATA[<p>Isaac Newton</p>]]></text>
    <answer><text>Laws of Motion</text></answer>
  </subquestion>
  <subquestion format="html">
    <text><![CDATA[<p>Albert Einstein</p>]]></text>
    <answer><text>Theory of Relativity</text></answer>
  </subquestion>
  <subquestion format="html">
    <text><![CDATA[<p>Alexander Fleming</p>]]></text>
    <answer><text>Penicillin</text></answer>
  </subquestion>
  <tags>
    <tag><text>year:2026</text></tag>
    <tag><text>subject:physics</text></tag>
    <tag><text>typology:matching-native</text></tag>
  </tags>
</question>
```

---

### 5.2 Matching via Standard MCQ (`type="multichoice"`)
```xml
<question type="multichoice">
  <name>
    <text>MA_02_HTML_Table_MCQ_Matching</text>
  </name>
  <questiontext format="html">
    <text><![CDATA[
      <p>Match the items in <strong>Column I</strong> with their corresponding descriptions in <strong>Column II</strong>:</p>
      
      <table border="1" cellpadding="6" cellspacing="0" style="border-collapse: collapse; width: 100%; text-align: left;">
        <thead>
          <tr style="background-color: #f2f2f2;">
            <th style="width: 50%;">Column I</th>
            <th style="width: 50%;">Column II</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>1. Isaac Newton</td>
            <td>A. Penicillin</td>
          </tr>
          <tr>
            <td>2. Albert Einstein</td>
            <td>B. Laws of Motion</td>
          </tr>
          <tr>
            <td>3. Alexander Fleming</td>
            <td>C. Theory of Relativity</td>
          </tr>
        </tbody>
      </table>
      
      <p><br />Choose the correct matching combination from the options below:</p>
    ]]></text>
  </questiontext>
  <generalfeedback format="html">
    <text><![CDATA[
      <p>The correct pairs are: 1-B (Newton -> Laws of Motion), 2-C (Einstein -> Relativity), 3-A (Fleming -> Penicillin).</p>
      <p><img src="@@PLUGINFILE@@/solution_chart.png" alt="Solution Table" width="200" height="100" /></p>
    ]]></text>
  </generalfeedback>
  <defaultgrade>1.0000000</defaultgrade>
  <penalty>0.3333333</penalty>
  <hidden>0</hidden>
  <single>true</single>
  <shuffleanswers>false</shuffleanswers>
  <answernumbering>abc</answernumbering>
  <answer fraction="0" format="html">
    <text><![CDATA[<p>1-A, 2-B, 3-C</p>]]></text>
    <feedback format="html">
      <text><![CDATA[<p>Incorrect. Newton did not discover Penicillin.</p>]]></text>
    </feedback>
  </answer>
  <answer fraction="100" format="html">
    <text><![CDATA[<p>1-B, 2-C, 3-A</p>]]></text>
    <feedback format="html">
      <text><![CDATA[
        <p>Correct match!</p>
      ]]></text>
    </feedback>
  </answer>
  <answer fraction="0" format="html">
    <text><![CDATA[<p>1-C, 2-A, 3-B</p>]]></text>
    <feedback format="html">
      <text><![CDATA[<p>Incorrect sequence.</p>]]></text>
    </feedback>
  </answer>
  <answer fraction="0" format="html">
    <text><![CDATA[<p>1-B, 2-A, 3-C</p>]]></text>
    <feedback format="html">
      <text><![CDATA[<p>Incorrect. Einstein did not discover Penicillin.</p>]]></text>
    </feedback>
  </answer>
  <tags>
    <tag><text>year:2026</text></tag>
    <tag><text>subject:history-of-science</text></tag>
    <tag><text>typology:matching-mcq</text></tag>
  </tags>
</question>
```

---

## 6. Numerical Questions (`type="numerical"`)

```xml
<question type="numerical">
  <name>
    <text>NUM_01_Physics_Work_Done</text>
  </name>
  <questiontext format="html">
    <text><![CDATA[
      <p>A constant horizontal force of $F = 50	ext{ N}$ is applied to drag a block across a floor over a displacement of $d = 4	ext{ m}$, as shown in the diagram below.</p>
      <p>Calculate the total work done $W$ on the block in Joules ($	ext{J}$).</p>
    ]]></text>
  </questiontext>
  <generalfeedback format="html">
    <text><![CDATA[
      <p>Work done is calculated using the formula: $$W = F 	imes d$$</p>
      <p>Substituting the given values:</p>
      <p>$$W = 50	ext{ N} 	imes 4	ext{ m} = 200	ext{ J}$$</p>
    ]]></text>
  </generalfeedback>
  <defaultgrade>1.0000000</defaultgrade>
  <penalty>0.3333333</penalty>
  <hidden>0</hidden>
  <answer fraction="100" format="moodle_auto_format">
    <text>200</text>
    <tolerance>0.5</tolerance>
    <feedback format="html">
      <text><![CDATA[
        <p>Correct! The work done is $200	ext{ J}$.</p>
      ]]></text>
    </feedback>
  </answer>
  <unitgradingtype>0</unitgradingtype>
  <unitpenalty>0.1000000</unitpenalty>
  <showunits>3</showunits>
  <unitsleft>0</unitsleft>
  <tags>
    <tag><text>year:2026</text></tag>
    <tag><text>subject:physics</text></tag>
  </tags>
</question>
```