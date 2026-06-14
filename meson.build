project('sway-systemd', [],
  meson_version: '>= 0.56',
  license: 'MIT',
)

enabled = get_option('autoload-configs')
configs = {
  'config.d/10-systemd-session.conf.in':        true,
  'config.d/10-systemd-cgroups.conf.in':        enabled.contains('all') or enabled.contains('cgroups'),
  'config.d/95-system-keyboard-config.conf.in': enabled.contains('all') or enabled.contains('locale1'),
  'config.d/95-xdg-desktop-autostart.conf.in':  enabled.contains('all') or enabled.contains('autostart'),
}

scripts = [
  'src/session.sh',
  'src/wait-sni-ready',
  'src/locale1-xkb-config',
]

unit_files = [
  'units/sway-session.target',
  'units/sway-session-shutdown.target',
  'units/sway-xdg-autostart.target',
]

systemd = dependency('systemd')
conf_dir = get_option('sysconfdir') / 'sway' / 'config.d'
data_dir = get_option('datadir') / meson.project_name()
# must be absolute path for configuration_data
exec_dir = get_option('prefix') / get_option('libexecdir') / meson.project_name()

install_data(
  scripts,
  install_dir: exec_dir,
  install_mode: 'rwxr-xr-x',
)

install_data(
  unit_files,
  install_dir: systemd.get_variable(pkgconfig: 'systemduserunitdir'),
)

conf_data = configuration_data()
conf_data.set('execdir', exec_dir)

foreach config, enabled : configs
  configure_file(
    configuration: conf_data,
    input: config,
    output: '@BASENAME@',
    install_dir: enabled ? conf_dir : data_dir,
  )
endforeach

# Build and install the assign-cgroups Rust binary
cargo = find_program('cargo')

if get_option('buildtype') == 'release'
  cargo_flags = '--release --locked'
  cargo_profile = 'release'
else
  cargo_flags = ''
  cargo_profile = 'debug'
endif

cargo_target_dir = meson.project_build_root() / 'cargo-target'
cargo_bin = cargo_target_dir / cargo_profile / 'assign-cgroups'

custom_target(
  'assign-cgroups',
  build_by_default: true,
  output: 'assign-cgroups',
  console: true,
  install: true,
  install_dir: exec_dir,
  command: [
    'sh', '-c',
    cargo.full_path() + ' build' +
    (cargo_flags != '' ? ' ' + cargo_flags : '') +
    ' --manifest-path ' + (meson.project_source_root() / 'Cargo.toml') +
    ' --target-dir ' + cargo_target_dir +
    ' && cp ' + cargo_bin + ' "$1"',
    '_',
    '@OUTPUT@',
  ],
)
