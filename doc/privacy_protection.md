# Privacy Protection Gaps

The current implementation performs metadata-driven redaction. It does not
guarantee that every privacy-sensitive region is detected or hidden.

This document covers four selected gaps from the privacy-gap review.

## 1. Incomplete Detection Coverage

ADAS metadata does not identify every visible person or vehicle. Objects
without supported metadata remain visible, even when privacy rendering
completes successfully.

Needed improvement: image-based detection to supplement camera metadata,
with coverage reporting and validation across representative recordings.

## 2. No Dedicated Forward-Face or License-Plate Detection

Forward rendering uses ADAS vehicle and pedestrian rectangles. It does not
separately detect forward-facing camera subjects' faces or license plates.

Blurring whole vehicle/pedestrian rectangles blocks too much content of the
frame, obscuring useful scene details beyond the sensitive face or plate.
It also does not protect faces or plates on objects absent from ADAS results.

Needed improvement: dedicated face and license-plate detection, with selective
redaction of those regions instead of entire vehicles or pedestrians.

## 3. Tracking Gap

When updates are missing, the rectangle stays still while the subject may
move outside it.

## 4. Timing Gap

A rectangle may be applied to the wrong video frame if metadata and video
timestamps are not correctly aligned.
