project('sway-systemd', [],
  meson_version: '>= 0.51',
  license: 'MIT',
)

configs = [
  'config.d/10-systemd-session.conf.in',
]

scripts = [
  'src/session.sh',
]

unit_files = [
  'sway-session.target',
]

if not get_option('cgroups').disabled()
  configs += 'config.d/10-systemd-cgroups.conf.in'
  scripts += 'src/assign-cgroups.py'
endif

if get_option('autostart')
  scripts += 'src/wait-sni-ready'
  unit_files += 'sway-xdg-autostart.target'
endif

if get_option('locale1')
  scripts += 'src/locale1-xkb-config'
endif

systemd = dependency('systemd')
conf_dir = join_paths(get_option('sysconfdir'), 'sway', 'config.d')
exec_dir = join_paths(
  get_option('prefix'),
  get_option('libexecdir'),
  meson.project_name(),
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
