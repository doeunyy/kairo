---
name: interview-scheduling
description: Find interview times from the user's calendar, check conflicts, and prepare for an upcoming interview.
version: 0.1.0
author: Kairo
license: MIT
metadata:
  hermes:
    tags: [Calendar, Job Search]
    related_skills: [recruiting-email, job-tracker, interview-weather]
    requires_toolsets: [mcp-kairo]
---

# Interview Scheduling

The calendar is read-only (secret ICS feed). Kairo can suggest times and check conflicts; the
user adds the event themselves once it is confirmed.

## When to Use

- A recruiter asked for availability, or the user asks "When am I free?" / "Does this time work?" (or in Korean).
- An interview is coming up and the user wants a prep summary.

## Quick Reference

| Need | Tool |
|---|---|
| Events in a range | `mcp_kairo_calendar_events(start_date, days)` |
| Free time on a day | `mcp_kairo_free_slots(day, minutes)` |
| Does a time clash? | `mcp_kairo_check_conflict(start, end)` |

Working hours 09:00–18:00 America/Los_Angeles, with 15 minutes kept free around events.

## Procedure

### Propose interview times
1. Default length 60 minutes unless the mail or user says otherwise.
2. Look at the next 5 weekdays starting tomorrow (`free_slots` per day).
3. Pick 3 options on different days, preferring late morning. Each must sit inside a free slot.
4. Present them as `Fri Nov 6, 10:00–11:00 AM PT`. If the recruiter is in another time zone and the
   user mentioned it, show both.
5. Offer to put them in a reply draft (recruiting-email skill).

### Check a proposed time
`check_conflict(start, end)`; if it clashes, name the event and offer the nearest free slot.

### Interview prep
1. `list_applications` → the row for this interview (role, JD summary, next action).
2. `calendar_events` for the interview day.
3. Give: time and how long until it starts, the role's 3 must-have skills from the JD summary,
   two questions to ask the interviewer, and the weather if it is on site.

## Pitfalls

- State exact dates for relative words ("tomorrow" → Thu Nov 5).
- The calendar may have no events; that means everything is free, not that reading failed.
- Never claim the event was added to the calendar.

## Verification

Every proposed slot must fall inside a `free_slots` range for that day.
