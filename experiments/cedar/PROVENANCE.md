# Build provenance

`prepare.py` fetches only these public repositories and verifies the detached
commit before creating the Docker context:

- [cedar-spec](https://github.com/cedar-policy/cedar-spec), commit
  `acb0db7daa838249d894d2af64c850e1c9bf0d7d`, Apache-2.0.
- [cedar](https://github.com/cedar-policy/cedar), commit
  `9502ae02564a23028c732f8c1f2311635394c34f`, Apache-2.0.
- Lean toolchain `leanprover/lean4:v4.34.0`, installed from the checked-in
  `lean-toolchain` file.
- CVC5 static release `1.2.1`, downloaded from its public GitHub release;
  CVC5's BSD-3-Clause license and notices remain upstream-owned.
- Amazon Linux 2023 base image digest
  `sha256:74c545e3e04db388b00bd31d7cc5640d4e9c12058a6d72af938d113da3c82893`.
- Rustup `1.28.2` and Elan installer commit
  `0e36a07b9bbcc5381fa6250df109f9a4f94d7bac`.

The Cedar sources are not patched. The build-specific changes are confined to
this recipe: it builds the checked-in Lake manifest without `lake update`, uses
the committed lockfiles for the two unversioned CLI/FFI manifests with
`cargo build --release --locked`, and exports Lean's `libleanshared.so` through
`LD_LIBRARY_PATH` in the minimal runtime. The latter is required because the
upstream binary does not carry a runtime search path in this container.

`evidence.json` records source/tool pins, fixture hashes, verifier hashes, and
the local image ID. It does not treat that local ID as a portable rebuild ID.
