# Adoback Homebrew Formula
#
# 安装方式:
#   brew tap SOULRAi/tap
#   brew install adoback
#
# 此文件由 CI/CD 自动更新版本号和 SHA256
# 也可手动放入 homebrew-tap 仓库: Formula/adoback.rb

class Adoback < Formula
  desc "macOS 本地 Adobe 项目文件备份守护工具"
  homepage "https://github.com/SOULRAi/adoback"
  version "0.5.8"
  license "MIT"

  depends_on :macos

  on_arm do
    url "https://github.com/SOULRAi/adoback/releases/download/v#{version}/adoback-macos-arm64"
    sha256 "PLACEHOLDER_ARM64_SHA256"
  end

  on_intel do
    url "https://github.com/SOULRAi/adoback/releases/download/v#{version}/adoback-macos-x86_64"
    sha256 "PLACEHOLDER_X86_64_SHA256"
  end

  def install
    binary_name = Hardware::CPU.arm? ? "adoback-macos-arm64" : "adoback-macos-x86_64"
    bin.install binary_name => "adoback"
  end

  def caveats
    <<~EOS
      Adoback 已安装！

      快速开始:
        adoback setup        一键初始化
        adoback --help       查看所有命令

      文档: https://github.com/SOULRAi/adoback
    EOS
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/adoback --version")
  end
end
