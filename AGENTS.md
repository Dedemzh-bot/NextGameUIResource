# NextGameUIResource maintenance

- Keep all machine paths in environment variables or ignored local `.env` configuration. Do not commit source images, Unreal assets, resource tables, generated manifests/readbacks, credentials or private local paths.
- Preserve the accepted 2+2+4 ID structure. Categories are project-configured, not a hardcoded game taxonomy; never renumber existing resource IDs or reuse reserved IDs.
- Prefix routing remains exact: gui/icon atlas, pic/por standalone; only icon/por register automatically unless explicitly requested otherwise.
- Keep source inputs unchanged; verify packed pixels and saved engine assets before resource-table writes. Preserve existing table bytes and protect atomic writes against concurrent runs.
- Import scripts run in the target Unreal Editor, not normal Python. Normal offline tests must not mutate real project assets or resource.txt.
- Run `python -m unittest discover -s tests -v` and `python -m unittest test_rules -v` for relevant changes. Use the current model and reasoning effort for all delegated work.
