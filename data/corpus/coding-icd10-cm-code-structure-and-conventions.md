---
doc_id: coding-icd10-cm-code-structure-and-conventions
category: coding
title: "ICD-10-CM code structure and the conventions that govern selection"
source: "US clinical modification of ICD-10 maintained by NCHS and CMS (educational summary)"
last_reviewed: 2026-08-17
---

> **Educational summary — not medical advice.** This note is reference material for a software
> project. It does not diagnose or treat, and it must not be used for real medical decisions.

## What a code looks like

An ICD-10-CM code is three to seven characters long. The first character is a letter, the second is
a digit, and the remaining characters may be either. A decimal point follows the first three
characters when more follow, so I10 has no decimal and E11.9 has one.

The three-character form is the category. Characters four and five add detail — cause, anatomical
site, severity — and character six frequently carries laterality, where right, left and unspecified
are three different codes rather than one code with a note. A seventh character, where a category
uses one, records something about the encounter rather than about the disease: for injuries it
distinguishes initial treatment, subsequent treatment and sequela.

Most categories are not valid at three characters. A code has to be carried to its full length for
the category it belongs to, and a category that requires a seventh character requires it even when
the intervening characters are empty — which is what the placeholder X exists for, holding a
position open so the seventh character lands in the seventh place.

## The two-step lookup

A code is found in the Alphabetic Index and then verified in the Tabular List. Skipping the second
step is the most common way to arrive at a wrong code, because the index does not carry the
instructional notes and the tabular list does. The tabular list governs.

## The instructional notes

**Excludes1** means "not coded here" in the strong sense: the two conditions cannot both be present,
and the codes are never reported together. **Excludes2** is the weaker one: the excluded condition
is not part of the code above it, but a person can have both, and both may be reported. The two look
alike on the page and mean opposite things about whether a second code is permitted.

**Code first** and **use additional code** appear as a pair and fix sequencing: an underlying
condition is reported before its manifestation. **Code also** signals that two codes are needed
without dictating which comes first.

**Combination codes** carry two conditions, or a condition and a complication, in one code. Where a
combination code exists it is used instead of the separate codes, which is why a family such as
diabetes appears so large.

## "With", and the presumption it creates

Where the Alphabetic Index links two conditions with the word "with", the classification reads it as
"associated with" or "due to" and presumes the relationship without requiring the record to state
it. The presumption holds only for the pairings the index actually lists, and it is overridden when
the documentation says the two are unrelated.

## Unspecified codes, NEC and NOS

**NEC**, not elsewhere classifiable, means the condition is specified but the classification has no
distinct code for it. **NOS**, not otherwise specified, means the documentation was not specific.
The distinction matters because an unspecified code is a statement about the record rather than
about the person, and it remains a valid code: where the documentation genuinely does not support
more detail, the unspecified code is the correct one, and inferring detail the record does not
contain is the error the convention exists to prevent.
