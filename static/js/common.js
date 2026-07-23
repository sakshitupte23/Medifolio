function   checkAuth(path,mode="") {
  try {
    const key = "loggedIn";
    let val = localStorage.getItem(key);

    // initialize key if it doesn't exist
    if (val === null) {
      localStorage.setItem(key, "false");
      val = "false";
    }

    // normalize value (handle "true"/"false" or boolean)
    const isLoggedIn = val === "true" || val === true;
    const finalState = mode === "!" ? !isLoggedIn : isLoggedIn;

    if (finalState) {
      window.location.href = path;
    }
  } catch (err) {
    console.error("localStorage unavailable:", err);
  }
}
