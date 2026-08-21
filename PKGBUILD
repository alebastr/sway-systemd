pkgname=sway-systemd
pkgver=0.4.1
pkgrel=3
pkgdesc="Systemd integration for Sway session"
arch=(any)
url="https://github.com/yegorius/sway-systemd"
license=("MIT")
depends=("sway" "dbus" "sway-contrib")
makedepends=("git" "meson")
conflicts=("sway-services-git")
source=("src.tar")
sha512sums=('SKIP')

prepare() {
  arch-meson "$srcdir" build
}

build() {
  meson compile -C build
}

#check() {
#  meson test -C build
#}

package() {
  meson install -C build --destdir "$pkgdir" --no-rebuild

  rm "$pkgdir/usr/lib/systemd/user/sway-session.target"

  cd "$srcdir"

  install -Dm644 "LICENSE" "${pkgdir}/usr/share/licenses/${pkgname}/LICENSE"
  install -Dm644 "README.md" "${pkgdir}/usr/share/doc/${pkgname}/README.md"
}
