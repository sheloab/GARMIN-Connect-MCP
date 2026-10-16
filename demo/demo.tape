# Regenerate the README demo GIF:
#   cd demo && vhs demo.tape
# Requires: vhs + ttyd + ffmpeg, opencode installed and authenticated.

Output ../docs/assets/demo.gif

Set Shell "bash"
Set FontSize 15
Set Width 1100
Set Height 620
Set Padding 20
Set Theme "Catppuccin Mocha"
Set TypingSpeed 20ms
Set PlaybackSpeed 2.2
Set WaitTimeout 120s

Sleep 500ms
Type "export DONE_MARKER='DEMO''_DONE'; clear"
Enter
Sleep 500ms
Type "opencode run 'My legs feel tired today. Check my Garmin recovery and tell me: easy day or intervals?' ; echo $DONE_MARKER"
Enter

Wait+Screen /DEMO_DONE/
Sleep 2500ms
