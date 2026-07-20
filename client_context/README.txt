This folder is gitignored. Copy these files here manually on the VM after cloning.

Files expected by analyze.py:

  program_brief.txt   — contents of Program_Context_Brief.md from your Workcall Drive
  rolodex.txt         — contents of 04_people_rolodex.md from your Workcall Drive
  solution_prompt.txt — the SOLUTION prompt block from PromptLibrary.md (the fenced code block only, not the ### heading)

All three are optional — the script falls back gracefully if any are missing, but quality
will be noticeably better with them in place, especially rolodex for name normalization.
