//! Reattach an existing local client tool server without resetting thread settings.
use crate::session::Session;
use codex_config::CloudConfigBundleBindingStatus;
use codex_config::McpServerConfig;
use codex_config::McpServerTransportConfig;
use codex_config::RawMcpServerConfig;
use std::io;
use std::sync::Arc;

fn local_endpoint(transport: &McpServerTransportConfig) -> bool {
    let McpServerTransportConfig::StreamableHttp {
        url,
        bearer_token_env_var: None,
        env_http_headers: None,
        http_headers_helper: None,
        ..
    } = transport
    else {
        return false;
    };
    url::Url::parse(url).is_ok_and(|url| {
        url.scheme() == "http"
            && url.host_str() == Some("127.0.0.1")
            && url.path() == "/mcp"
            && url.username().is_empty()
            && url.password().is_none()
            && url.query().is_none()
            && url.fragment().is_none()
    })
}

impl Session {
    pub(crate) async fn rebind_client_mcp_server(
        &self,
        name: &str,
        value: toml::Value,
    ) -> io::Result<()> {
        let raw: RawMcpServerConfig = value
            .clone()
            .try_into()
            .map_err(|_| io::Error::other("Invalid client MCP configuration"))?;
        let incoming = McpServerConfig::try_from(raw)
            .map_err(|_| io::Error::other("Invalid client MCP configuration"))?;
        let mut state = self.state.lock().await;
        let mut config = state
            .session_configuration
            .original_config_do_not_use
            .as_ref()
            .clone();
        let existing = config
            .mcp_servers
            .get()
            .get(name)
            .ok_or_else(|| io::Error::other("Client MCP server is unavailable"))?;
        let mut policy = incoming.clone();
        policy.transport = existing.transport.clone();
        if &policy != existing
            || !local_endpoint(&incoming.transport)
            || !local_endpoint(&existing.transport)
        {
            return Err(io::Error::other(
                "Client MCP rebinding may change only a local endpoint and its headers",
            ));
        }
        if existing == &incoming {
            return Ok(());
        }
        let binding = config.config_layer_stack.cloud_config_binding().cloned();
        let guard = binding.as_ref().map(|binding| binding.read());
        if guard
            .as_ref()
            .is_some_and(|guard| guard.status != CloudConfigBundleBindingStatus::Current)
        {
            return Err(io::Error::other(
                "Managed MCP policy is not current; retry after policy refresh",
            ));
        }
        config.config_layer_stack = config
            .config_layer_stack
            .with_rebound_session_mcp_server(name, value)?;
        let mut servers = config.mcp_servers.get().clone();
        servers.insert(name.to_owned(), incoming);
        config.mcp_servers.set(servers).map_err(|_| {
            io::Error::new(
                io::ErrorKind::PermissionDenied,
                "Managed policy rejects the replacement client MCP endpoint",
            )
        })?;
        state.session_configuration.original_config_do_not_use = Arc::new(config);
        self.services.mcp_runtime.invalidate_resource_caches();
        self.mark_mcp_runtime_dirty();
        drop(guard);
        drop(state);
        self.schedule_mcp_prewarm();
        Ok(())
    }
}
