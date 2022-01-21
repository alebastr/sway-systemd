project('sway-systemd', [],
  meson_version: '>= 0.49',
  license: 'MIT'
)

systemd = dependency('systemd')
conf_dir = join_paths(get_option('sysconfdir'), 'sway', 'config.d')
exec_dir = join_paths(
  get_option('prefix'),
  get_option('libexecdir'),
  meson.project_name()
)

conf_data = configuration_data()
conf_data.set('execdir', exec_dir)

install_data(
  'sway-session.target',
  'sway-session@.target',
  install_dir: systemd.get_pkgconfig_variable('systemduserunitdir')
)

install_data('src/session.sh',
  install_dir: exec_dir,
  install_mode: 'rwxr-xr-x',
)

configure_file(
  configuration: conf_data,
  input: 'config.d/10-systemd-session.conf.in',
  output: '@BASENAME@',
  install_dir: conf_dir,
)

if not get_option('cgroups').disabled()
  install_data('src/assign-cgroups.py',
    install_dir: exec_dir,
    install_mode: 'rwxr-xr-x',
  )

  configure_file(
    configuration: conf_data,
    input: 'config.d/10-systemd-cgroups.conf.in',
    output: '@BASENAME@',
    install_dir: conf_dir,
  )
endif
