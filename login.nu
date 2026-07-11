$env.TS_USER = "Matthew.Rixman@Fiserv.com"

if "TS_PW" not-in $env {
    echo "password:"
    $env.TS_PW = (input -s)
}

uv run trainee-sim connect https://fiservcorp.sharepoint.com/sites/fuel-home/
uv run trainee-sim wait-for --url login.microsoftonline.com
uv run trainee-sim type $env.TS_USER --into "input#i0116"
uv run trainee-sim click --element "input#idSIButton9"
uv run trainee-sim wait-for --element "input[type=password]"
uv run trainee-sim type --into "input[type=password]" --env TS_PW --secret
uv run trainee-sim click --element "input#idSIButton9"

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
