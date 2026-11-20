# Generate a mikroSDK 2.0 Click driver

The generation unit is a board slug. A Click can use several components and
PDFs, and a component PDF can cover different boards and variants. The generator
joins those sources to the board profile and its schematic.

## Run

Supply a Sheetforge corpus and a local SDK checkout. The SDK directory can
contain both `mikrosdk_click_v2` and `mikrosdk_v2` repositories, or point
directly to the Click checkout for a structural validation run.

```bash
python -m pip install -e '.[dev]'
cp .env.sample .env
# Set MIMO_PAYG_API_KEY in the local .env for automated generation.
sheetforge-driver \
  --corpus /path/to/out-click \
  --sdk /path/to/mikrosdk \
  --board batt-boost-click \
  --model mimo-v2.6-pro \
  --compare-reference
```

The live client uses the standard MiMo API. Token Plan accounts can use
`--provider exchange`: run the command, open the fingerprinted request in
a compatible interactive coding tool configured for MiMo, save the JSON
completion to the named reply path, and rerun the same command. The
exchange files contain the prompts and responses without API credentials.

Use repeated `--board` arguments or `--all` with an indexed corpus and
`--limit` to start with a bounded batch. Automated batches use the standard
API and can incur model charges. `--attempts` sets the limit for each stage.
Use `--stem` to resolve local SDK naming differences for a single board.

## Generation loop

1. Retrieve board pinout and schematic evidence first. Search the board's
   document pages for timing, registers, startup, conversion and restrictions.
   Include a second text layout for table evidence where the corpus has it.
   Remove local paths and the saved website's target driver examples.
2. Ask MiMo for a hardware plan with quotes and passage IDs. Check that quotes
   exist in their cited passages and operations cite plan claims. Search again
   for blocking gaps. A remaining blocking gap stops package generation.
3. Generate the library, header, mikroBUS mapping, C example, CMake files,
   import marker, README, and NECTO manifests. Other Click drivers provide
   SDK conventions. A trace connects plan claims to code symbols.
4. Validate the artifact layout, declarations, implementations and package
   metadata. Feed specific findings back to MiMo within the retry limit.
   `--build-harness` can run an explicitly chosen trusted local builder.
   The supplied pilot uses a fixed ARM build invocation and skips generated
   CMake at this stage.
5. With `--compare-reference`, compare the validated result against the
   installed target driver after generation. The reference comparison measures
   common API names, signatures, register constants, and normalized C text.
   The original target source is withheld from the prompt.

## Outputs

Each board directory includes `plan.json`, `citations.json`, `trace.json`
and `report.json`. On success, `driver/<stem>/` is the generated package.
An accepted hardware plan is reusable after a package failure. Use
`--reviewed-plan plan.json` to import a locally reviewed plan for one board.
Its source quotes and operation references go through the same validation. A validated
package is reusable while its inputs and artifact hashes match.

Verbatim quotes and a passing cross-build are evidence for review. Check
derived values, sequencing, timing and actual bus behavior on the target
hardware before deployment. Similarity scores measure resemblance to the
reference implementation, and exact API compatibility needs its own check.

Use `--reference-api` for a separate compatibility run. This mode supplies the
reference header's declarations and context types to the implementation
stage, then requires matching declarations. Keep its scores separate from the
blind benchmark. Both modes use the board evidence for hardware values.
