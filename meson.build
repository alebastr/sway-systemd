project('sway-systemd', ['c'],
  version : '0.4.1',
  default_options : ['warning_level=3', 'c_std=c2x'],
  meson_version: '>= 0.57',
  license: 'MIT',
)

configs = [
  'config.d/10-systemd-session.conf.in',
  'config.d/95-system-keyboard-config.conf.in',
  'config.d/95-xdg-desktop-autostart.conf.in',
]

scripts = [
  'src/session.sh',
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

libsystemd = dependency('libsystemd')
deps = [libsystemd]

executable(
  'wait-sni-ready',
  'src/wait-sni-ready.c',
  dependencies : deps,
  install : true,
  install_dir : exec_dir
)

executable(
  'locale1-xkb-config',
  'src/locale1-xkb-config.c',
  dependencies: deps,
  install: true,
  install_dir: exec_dir
)

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

foreach config : configs
  configure_file(
    configuration: conf_data,
    input: config,
    output: '@BASENAME@',
    install_dir: conf_dir,
  )
endforeach
