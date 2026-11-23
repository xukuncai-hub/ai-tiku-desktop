#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    // 规避 Linux NVIDIA + WebKitGTK 2.54 DMA-BUF 合成导致的界面假死/无响应
    if std::env::var_os("WEBKIT_DISABLE_DMABUF_RENDERER").is_none() {
        unsafe {
            std::env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", "1");
        }
    }
    ai_tiku_desktop_lib::run()
}
