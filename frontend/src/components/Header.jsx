import {
  AccountCircle,
  Logout,
} from "@mui/icons-material";
import {
  AppBar,
  Box,
  Button,
  Chip,
  Toolbar,
  Typography,
} from "@mui/material";
import { useNavigate } from "react-router-dom";

import { useAuth } from "../context/AuthContext";

const drawerWidth = 240;

const Header = () => {
  const navigate = useNavigate();
  const { user, logout } = useAuth();

  const handleLogout = () => {
    logout();
    navigate("/login");
  };

  return (
    <AppBar
      position="fixed"
      sx={{
        width: `calc(100% - ${drawerWidth}px)`,
        ml: `${drawerWidth}px`,
        zIndex: (theme) =>
          theme.zIndex.drawer + 1,
      }}
    >
      <Toolbar>
        <Typography
          variant="h6"
          sx={{
            flexGrow: 1,
            fontWeight: 600,
          }}
        >
          KoalaTech | Automatic deployment 9.3C
        </Typography>

        <Box
          sx={{
            display: "flex",
            alignItems: "center",
            gap: 2,
          }}
        >
          <Chip
            icon={<AccountCircle />}
            label={
              user?.email ||
              user?.sub ||
              "User"
            }
            color="default"
            sx={{
              backgroundColor:
                "rgba(255,255,255,0.15)",
              color: "white",
              "& .MuiChip-icon": {
                color: "white",
              },
            }}
          />

          <Chip
            label={user?.role || "user"}
            size="small"
            sx={{
              textTransform: "capitalize",
              backgroundColor:
                "rgba(255,255,255,0.15)",
              color: "white",
            }}
          />

          <Button
            color="inherit"
            startIcon={<Logout />}
            onClick={handleLogout}
          >
            Logout
          </Button>
        </Box>
      </Toolbar>
    </AppBar>
  );
};

export default Header;