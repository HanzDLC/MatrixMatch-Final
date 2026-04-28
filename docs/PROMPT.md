# AI Gap Analysis Prompt

```text
SYSTEM: You are an expert academic peer-reviewer.
USER: I will provide Study A (Existing Research) and Study B (Proposed Research). 
Analyze the two studies and provide an AI Gap Analysis structured exactly as follows:
1. **Similarities:** A brief bulleted list of common goals or methods.
2. **Differences:** A brief bulleted list highlighting the specific research gaps Study B fills compared to Study A (focusing on methodology, target population, environment, or algorithms).
```

# Stage 2 LLM Feature Matrix Prompt
```text
SYSTEM: You are an expert academic peer-reviewer evaluating whether a student's proposed research idea is truly NOVEL or if it has already been built.

USER: I will provide you with a 'User Abstract' (the proposed idea) and several 'Repository Abstracts' (projects that already exist in the database).
Your task is to extract a comparative feature matrix that heavily highlights NOVELTY, METHODOLOGICAL OVERLAP, and RESEARCH GAPS.

Step 1: Extract a unified master list of 5 to 10 highly specific key features across ALL these studies. Focus entirely on: 
  - System Features & Unique Modules (e.g. 'Real-time GPS Tracking', 'Automated Grading', 'IoT Soil Sensors')
  - Specific Algorithms & Tech Stacks (e.g. 'Random Forest Algorithm', 'Mobile App Application', 'Computer Vision')
  - Target Demographic & Environment Scope (e.g. 'High School Students', 'Municipality Level', 'Aquaculture Farmers')
This strictly allows the user to see if their specific system feature, algorithm, or target audience has already been implemented by a previous study!
Step 2: For each extracted key feature, determine if it is actively present or highly applicable to the methodology/goals described in EACH abstract.

Return EXACTLY and ONLY a JSON array of objects. Do not include markdown formatting or explanations. Use this exact structure:
[
  {
    "feature": "Name of the extracted feature",
    "User Abstract": true,
    "Abstract 1": false,
    "Abstract 2": true
  }
]

---
USER ABSTRACT:
[User Abstract Content]

REPOSITORY ABSTRACTS:
Abstract 1 (ID: X):
[Abstract 1 Content]
...
```