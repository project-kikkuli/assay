# Public, pinned build for experiments/cedar. The build context is produced by
# prepare.py; it contains only public Cedar sources and committed Cargo locks.
FROM amazonlinux:2023@sha256:74c545e3e04db388b00bd31d7cc5640d4e9c12058a6d72af938d113da3c82893 AS build

ARG RUST_TOOLCHAIN=1.91.1
ARG RUSTUP_VERSION=1.28.2
ARG ELAN_COMMIT=0e36a07b9bbcc5381fa6250df109f9a4f94d7bac
ARG CVC5_VERSION=1.2.1
ENV CEDAR_SPEC_ROOT=/opt/src/cedar-spec
ENV PATH=/root/.elan/bin:/root/.cargo/bin:${PATH}

RUN yum update -y \
    && yum install -y curl-minimal clang tar zip unzip python3 git xz make wget gcc gcc-c++ protobuf-compiler \
    && yum clean all \
    && rm -rf /var/cache/yum

RUN ARCH=$(uname -m) \
    && case "${ARCH}" in x86_64) RUSTUP_ARCH=x86_64-unknown-linux-gnu ;; aarch64) RUSTUP_ARCH=aarch64-unknown-linux-gnu ;; *) exit 1 ;; esac \
    && wget -q https://static.rust-lang.org/rustup/archive/${RUSTUP_VERSION}/${RUSTUP_ARCH}/rustup-init \
    && install -m 0755 rustup-init /usr/local/bin/rustup-init \
    && rustup-init -y --default-toolchain ${RUST_TOOLCHAIN} \
    && rustup component add llvm-tools-preview rust-src \
    && rm rustup-init

RUN wget -q https://raw.githubusercontent.com/leanprover/elan/${ELAN_COMMIT}/elan-init.sh \
    && sh elan-init.sh -y --default-toolchain none \
    && rm elan-init.sh

RUN ARCH=$(uname -m) \
    && case "${ARCH}" in x86_64) CVC5_ARCH=x86_64 ;; aarch64) CVC5_ARCH=arm64 ;; *) exit 1 ;; esac \
    && wget -q https://github.com/cvc5/cvc5/releases/download/cvc5-${CVC5_VERSION}/cvc5-Linux-${CVC5_ARCH}-static.zip \
    && unzip -q cvc5-Linux-${CVC5_ARCH}-static.zip \
    && install -m 0755 cvc5-Linux-${CVC5_ARCH}-static/bin/cvc5 /usr/local/bin/cvc5 \
    && rm -rf cvc5-Linux-${CVC5_ARCH}-static cvc5-Linux-${CVC5_ARCH}-static.zip

COPY cedar-spec ${CEDAR_SPEC_ROOT}
COPY provenance.json /opt/cedar-provenance.json
RUN python3 -c 'import json; p=json.load(open("/opt/cedar-provenance.json")); assert p["cedar_spec_commit"] == "acb0db7daa838249d894d2af64c850e1c9bf0d7d"; assert p["cedar_dependency_commit"] == "9502ae02564a23028c732f8c1f2311635394c34f"'

WORKDIR ${CEDAR_SPEC_ROOT}
RUN elan toolchain install "$(tr -d "\n" < cedar-lean/lean-toolchain)" \
    && elan default "$(tr -d "\n" < cedar-lean/lean-toolchain)"

# This is intentionally the checked-in Lake manifest, without the upstream
# build_lean_lib.sh `lake update`; package revisions are therefore fixed by
# lake-manifest.json.
WORKDIR ${CEDAR_SPEC_ROOT}/cedar-lean
RUN lake build Cedar:static Protobuf:static CedarProto:static Cedar.SymCC:static CedarFFI:static Batteries:static

WORKDIR ${CEDAR_SPEC_ROOT}/cedar-lean-cli
RUN . ../cedar-lean-ffi/set_env_vars.sh \
    && cargo build --release --locked

FROM amazonlinux:2023@sha256:74c545e3e04db388b00bd31d7cc5640d4e9c12058a6d72af938d113da3c82893 AS runtime
COPY --from=build /opt/src/cedar-spec/cedar-lean-cli/target/release/cedar-lean-cli /usr/local/bin/cedar-lean-cli
COPY --from=build /root/.elan/toolchains/leanprover--lean4---v4.34.0/lib/lean/libleanshared.so /usr/local/lib/libleanshared.so
COPY --from=build /usr/local/bin/cvc5 /usr/local/bin/cvc5
ENV CVC5=/usr/local/bin/cvc5
ENV LD_LIBRARY_PATH=/usr/local/lib
ENTRYPOINT ["cedar-lean-cli"]
