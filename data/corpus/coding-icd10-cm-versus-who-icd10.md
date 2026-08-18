---
doc_id: coding-icd10-cm-versus-who-icd10
category: coding
title: "ICD-10-CM and WHO ICD-10: why a code found in one may not exist in the other"
source: "US clinical modification of ICD-10 maintained by NCHS and CMS (educational summary)"
last_reviewed: 2026-08-17
---

> **Educational summary — not medical advice.** This note is reference material for a software
> project. It does not diagnose or treat, and it must not be used for real medical decisions.

## Two things share one name

ICD-10 is the World Health Organization's international classification of diseases, used across
countries chiefly for mortality and morbidity statistics. ICD-10-CM is the United States clinical
modification of it, developed and maintained by the National Center for Health Statistics, and it is
what US diagnosis coding actually uses.

The relationship is one of extension rather than translation. The clinical modification keeps the
chapter structure, the letter ranges and most of the three-character categories of the international
version, and then subdivides them much further. A question answered from a WHO ICD-10 reference can
therefore be right about the chapter and wrong about the code.

## What the modification adds

Three additions account for most of the difference in size.

**Laterality.** A large number of categories distinguish right, left, bilateral and unspecified,
which the international version generally does not. This alone multiplies the site-specific
categories in the musculoskeletal and injury chapters.

**Seventh characters.** Injuries, and some obstetric and other categories, carry a character
recording the episode of care rather than anything about the condition — initial encounter,
subsequent encounter, sequela — so one injury generates several valid codes across an episode.

**Greater clinical detail.** Severity, acuteness, laterality and complication status are pushed into
the code itself rather than left to a separate field, and combination codes fold pairs of conditions
together. The diabetes and asthma categories are the clearest examples.

## Procedures are a separate system

The clinical modification covers diagnoses only. Inpatient hospital procedures are coded in
ICD-10-PCS, a structurally unrelated system maintained by the Centers for Medicare and Medicaid
Services in which every code is seven characters and each position carries a fixed meaning. Neither
the WHO classification nor the diagnosis modification contains it, and outpatient procedure coding
in the US uses a different system again.

## Updates, and what that means for a stored answer

The clinical modification is revised on an annual cycle, with changes taking effect at the start of
the federal fiscal year on October 1. Categories are added, subdivided and occasionally retired, so
a code that was valid in one year may not be valid in another. Any stored or cached code answer
carries an implicit date, and a lookup against a current file is the only thing that settles
whether a code is still valid.

## Other national modifications, and ICD-11

Several other countries maintain their own clinical modifications of the same international
classification, with their own subdivisions, so "the ICD-10 code" is an underspecified phrase
without a country attached. The World Health Organization has since released ICD-11, which is a
different classification with a different structure rather than a further revision of this one; the
United States has not adopted it for diagnosis coding, and codes do not carry across between them.
