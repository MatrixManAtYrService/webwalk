$env.FISERV_USER = "Matthew.Rixman@Fiserv.com"
$env.CLOVER_EMAIL = "matt.rixman@clover.com"

if "TS_PW" not-in $env {
    echo "password:"
    $env.TS_PW = (input -s)
}

uv run trainee-sim connect https://fiservcorp.sharepoint.com/sites/fuel-home/

# First wait for the redirect to Microsoft — generous, but it returns the moment
# the URL matches, so it costs nothing when the redirect is quick.
uv run trainee-sim wait-for --url login.microsoftonline.com --timeout 60

# Azure AD is username-first, but a remembered profile jumps STRAIGHT to the
# password page (skipping email). Once we're on the MS page the form paints
# fast, so probe for the email field with a SHORT timeout: fill it if present,
# and let `try` swallow the quick timeout on the skip-to-password path (this is
# what used to stall ~20s before the password went in).
try {
    uv run trainee-sim wait-for --element "input#i0116" --timeout 6
    uv run trainee-sim type $env.FISERV_USER --into "input#i0116"
    uv run trainee-sim click --element "input#idSIButton9"
}

# Either path lands on the password page (field id i0118). Gate on that element,
# not the URL, so we don't type into a half-rendered form.
uv run trainee-sim wait-for --element "input#i0118" --timeout 60
uv run trainee-sim type --into "input#i0118" --env TS_PW --secret
uv run trainee-sim click --element "input#idSIButton9"

# Microsoft often interposes a "Stay signed in?" (KMSI) prompt before letting us
# through. Accept it (click "Yes" = idSIButton9) if present; non-fatal when the
# tenant skips it. The KMSI checkbox id is a stable marker for that page.
try {
    uv run trainee-sim wait-for --element "#KmsiCheckboxField" --timeout 10
    uv run trainee-sim click --element "input#idSIButton9"
}

# Land on FUEL home, then launch Xchange (opens the Cornerstone LMS in a new tab).
uv run trainee-sim wait-for --element 'a[href*="signin/Xchange"]' --timeout 60
uv run trainee-sim click --element 'a[href*="signin/Xchange"]'

# In the newly opened LMS tab, open the Secure Coding Training curriculum.
# Anchored on the title via aria-label (year-agnostic; NOT by list position,
# which shifts with the past-due / due-soon dates).
uv run trainee-sim wait-for --window latest --element 'a[aria-label*="Open Curriculum"][aria-label*="Secure Coding Training"]' --timeout 60
uv run trainee-sim click --window latest --element 'a[aria-label*="Open Curriculum"][aria-label*="Secure Coding Training"]'

# Launch the course content (opens the player; the course itself loads in an iframe).
uv run trainee-sim wait-for --window latest --by xpath --element '//button[normalize-space(.)="Launch"]' --timeout 60
uv run trainee-sim click --window latest --by xpath --element '//button[normalize-space(.)="Launch"]'

# The player nests the launcher two frames deep. Click the inner "Launch Course"
# button (frame 3/1) — this opens Secure Code Warrior (the course host) in a new tab.
uv run trainee-sim wait-for --window latest --frame "3/1" --by xpath --element '//button[normalize-space(.)="Launch Course"]' --timeout 60
uv run trainee-sim click --window latest --frame "3/1" --by xpath --element '//button[normalize-space(.)="Launch Course"]'

# New tab: Secure Code Warrior. Email-first SSO login (uses the Clover email).
uv run trainee-sim wait-for --window latest --element "input#email" --timeout 60
uv run trainee-sim click --window latest --by xpath --element '//button[normalize-space(.)="Deny Non-Essential"]'
uv run trainee-sim type $env.CLOVER_EMAIL --window latest --into "input#email"
# Target the Next button by its stable attribute, NOT visible text: CSS lowercases
# the label ("next") but the DOM text is "Next", so a text selector wouldn't match.
uv run trainee-sim click --window latest --element 'button[translate="NEXT"]'

# Next triggers SAML SSO back to Microsoft, which prompts for an MFA code.
# Wait for that prompt, then hand off to the human — the code comes from an
# authenticator app and cannot (and should not) be scripted.
uv run trainee-sim wait-for --window latest --element "input#idTxtBx_SAOTCC_OTC" --timeout 90
print ""
print "  ============================================================"
print "  ENTER YOUR MFA CODE in the browser on the target machine,"
print "  then submit it. Secure Code Warrior will load the course."
print "  ============================================================"
print ""
# Block through the manual MFA until Secure Code Warrior lands on the course's
# language list (the "Supported Languages" page). The generous timeout covers
# the time it takes the human to enter the code + the SSO redirects.
uv run trainee-sim wait-for --window latest --url "courses/course-list" --timeout 180
print ""
print "  Secure Code Warrior course is loaded. You're in."
print "  (The language list is inside frame 0 of this tab.)"

# Auth is done, so start recording — everything from here is real training.
uv run trainee-sim telemetry on

# Open the Python Basic module. Matched by name inside frame 0, so it works
# whether Python Basic sits under "Resume your course" or "Select an available
# course" (and regardless of the % progress shown next to it).
uv run trainee-sim wait-for --window latest --frame 0 --by xpath --element '//a[contains(normalize-space(.), "Python Basic")]' --timeout 60
# --wait-after lets the breakdown page render before the after-snapshot, so the
# capture shows the real content (the "Resume" button) not a half-built page.
uv run trainee-sim click --window latest --frame 0 --by xpath --element '//a[contains(normalize-space(.), "Python Basic")]' --wait-after '//button' --wait-after-timeout 15
