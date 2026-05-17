fn main() {
    println!("cargo::rustc-check-cfg=cfg(has_nnue)");
    let weights = std::path::Path::new("src/weights.bin");
    if weights.exists() {
        println!("cargo:rustc-cfg=has_nnue");
    }
    println!("cargo:rerun-if-changed=src/weights.bin");
}
