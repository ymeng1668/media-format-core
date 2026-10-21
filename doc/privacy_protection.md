# Privacy Protection Gaps

The existing `render_privacy()` implementation performs metadata-driven
redaction. An optional `CorePythonBridge` now supports image-based face/plate
processing via the privacy-redaction plugin. Consumers must explicitly
feed decoded RGB frames; the legacy rendering path is not silently changed.
Neither path guarantees detection or hiding of every privacy-sensitive region.

This document covers four selected gaps from the privacy-gap review.

## 1. Incomplete Detection Coverage

ADAS metadata does not identify every visible person or vehicle. Objects
without supported metadata remain visible, even when privacy rendering
completes successfully.

Needed improvement: image-based detection to supplement camera metadata,
with coverage reporting and validation across representative recordings.

Implementation available: the optional Python package runs local YuNet and
vehicle-scoped plate inference with readiness/region metrics. Representative
camera-specific recall/false-masking qualification remains required.

## 2. No Dedicated Forward-Face or License-Plate Detection

Forward rendering uses ADAS vehicle and pedestrian rectangles. It does not
separately detect forward-facing camera subjects' faces or license plates.

Blurring whole vehicle/pedestrian rectangles blocks too much content of the
frame, obscuring useful scene details beyond the sensitive face or plate.
It also does not protect faces or plates on objects absent from ADAS results.

Needed improvement: dedicated face and license-plate detection, with selective
redaction of those regions instead of entire vehicles or pedestrians.

Implementation available: selective face/plate masks, blur and diagnostic boxes.
Vehicle context may come from caller-provided camera tracks or the optional
object tracker. A missing/misclassified vehicle can still suppress its plate
search; this is not a blanket privacy-coverage certification.

## 3. Tracking Gap

When updates are missing, the rectangle stays still while the subject may
move outside it.

The image path has temporal tracking and bounded prediction, separate from the
legacy held metadata rectangles. Motion/occlusion cases still need qualification.

## 4. Timing Gap

A rectangle may be applied to the wrong video frame if metadata and video
timestamps are not correctly aligned.

The image path processes the supplied frame directly and preserves its exact
frame index and microsecond PTS. Source ordering, seek resets and correct caller
metadata remain part of the integration contract. Legacy AI/video matching is
unchanged.
